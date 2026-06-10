"""Shared helpers for the reminder tools.

``_parse_iso`` is the strict ISO-8601 parser: it accepts only what
``datetime.fromisoformat`` accepts and defaults naive timestamps to UTC.
``snooze`` uses it directly for DB-owned ``fire_at`` values — a corrupt
row must fail loudly, never be fuzzy-guessed.

``_parse_when`` wraps it for USER-supplied ``when_iso`` strings (create,
accountability): ISO-strict first, then a dateparser fallback for natural
language in en/uk/ru. The model is still instructed to compute ISO from
the ``# now`` block — the fallback is a safety net for the rare phrase
that slips through verbatim, not the front door.

``_VALID_REPEAT`` enumerates the simple repeat keywords; anything
starting with ``RRULE:`` is also accepted (free-form advanced
schedules are validated downstream by the scheduler).
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def _parse_iso(s: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=UTC)
        return d
    except (ValueError, TypeError):
        return None


# dateparser quirks the fallback papers over (verified against 1.4.0):
#  - a trailing bare hour after at/о/в is silently dropped ("завтра о 9"
#    parses as just "завтра") — appending ":00" makes it stick;
#  - uk "через годину/хвилину" needs an explicit count ("через 1 годину").
_BARE_HOUR_RE = re.compile(r"(?<=\s)(at|о|в|у)\s+(\d{1,2})\s*$", re.IGNORECASE)
_UK_IMPLICIT_ONE_RE = re.compile(r"через\s+(годину|хвилину)", re.IGNORECASE)


def _normalize_nl(s: str) -> str:
    s = _BARE_HOUR_RE.sub(lambda m: f"{m.group(1)} {m.group(2)}:00", s)
    s = _UK_IMPLICIT_ONE_RE.sub(lambda m: f"через 1 {m.group(1)}", s)
    return s


def _parse_when(s: str, *, tz_name: str | None = None) -> datetime | None:
    """Parse a user-facing 'when' string: ISO-strict first, NL fallback.

    Relative/NL phrases resolve against now in the user's local tz (the same
    ``_resolve_local_tz_name`` source that builds the ``# now`` block, so the
    model's arithmetic and this fallback agree on "local"). Returns tz-aware
    UTC, or None when neither parser can read it. Best-effort by design —
    the model's ISO path is primary; unparseable phrases refuse loudly.
    """
    iso = _parse_iso(s)
    if iso is not None:
        return iso
    if not s or not s.strip():
        return None
    s = _normalize_nl(s.strip())

    import dateparser

    from agents.hooks import _resolve_local_tz_name

    tz = tz_name or _resolve_local_tz_name()
    try:
        zone = ZoneInfo(tz)
    except Exception:
        zone = UTC
        tz = "UTC"
    dt = dateparser.parse(
        s,
        languages=["en", "uk", "ru"],
        settings={
            "TIMEZONE": tz,
            "TO_TIMEZONE": "UTC",
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": datetime.now(zone).replace(tzinfo=None),
        },
    )
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


_VALID_REPEAT = {None, "", "daily", "weekly", "monthly", "yearly"}
