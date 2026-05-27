"""Recurrence rule engine for Hikari reminders.

Supports these cadences:
  - "daily"                  — +1 day, same wall time
  - "weekly:MON,WED,FRI"    — next listed weekday at same wall time
  - "monthly:1"              — first of next month
  - "monthly:last"           — last day of next month
  - "yearly:MM-DD"           — annual date
  - "every_n_days:N"         — +N days (N ≥ 1)
  - "every_n_hours:N"        — +N hours (1 ≤ N ≤ 168)
  - "every_n_minutes:N"      — +N minutes (1 ≤ N ≤ 1440)

Sub-day cadences (hours/minutes) use absolute UTC arithmetic — they
intentionally do *not* preserve wall-clock time across DST. Day-based
cadences continue to preserve wall-clock time via local-zone arithmetic.

End-of-month clamping (e.g. Jan 31 → Feb 28/29) uses
``calendar.monthrange``.  Invalid rule strings raise ``ValueError``
immediately — no silent coercion.
"""
from __future__ import annotations

import calendar
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ---------------------------------------------------------------------------
# Rule grammar
# ---------------------------------------------------------------------------

# Weekday names → isoweekday() values (Mon=1 … Sun=7)
_WEEKDAY_MAP: dict[str, int] = {
    "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6, "SUN": 7,
}

_RE_WEEKLY = re.compile(
    r"^weekly:(?P<days>[A-Z]{3}(?:,[A-Z]{3})*)$", re.IGNORECASE
)
_RE_MONTHLY_N = re.compile(r"^monthly:(?P<day>\d{1,2})$")
_RE_MONTHLY_LAST = re.compile(r"^monthly:last$", re.IGNORECASE)
_RE_YEARLY = re.compile(r"^yearly:(?P<month>\d{2})-(?P<day>\d{2})$")
_RE_EVERY_N = re.compile(r"^every_n_days:(?P<n>\d+)$")
_RE_EVERY_N_HR = re.compile(r"^every_n_hours:(?P<n>\d+)$")
_RE_EVERY_N_MIN = re.compile(r"^every_n_minutes:(?P<n>\d+)$")

# Accepted by reminder_create; anything else should raise ValueError.
VALID_RULE_RE = re.compile(
    r"^("
    r"daily"
    r"|weekly:[A-Z]{3}(?:,[A-Z]{3})*"
    r"|monthly:(\d{1,2}|last)"
    r"|yearly:\d{2}-\d{2}"
    r"|every_n_days:\d+"
    r"|every_n_hours:\d+"
    r"|every_n_minutes:\d+"
    r")$",
    re.IGNORECASE,
)


def _home_tz() -> ZoneInfo:
    """Read HOME_TZ env (or scheduler.timezone config) → ZoneInfo.

    Falls back to UTC on lookup failure so the engine never crashes —
    DST fidelity is best-effort when the host is misconfigured.
    """
    tz_name = (os.environ.get("HOME_TZ") or "").strip()
    if not tz_name:
        try:
            from agents import config as cfg
            tz_name = str(cfg.get("scheduler.timezone") or "")
        except Exception:
            pass
    tz_name = tz_name or "UTC"
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        return ZoneInfo("UTC")


def validate_rule(rule: str) -> None:
    """Raise ``ValueError`` if *rule* does not match the recurrence grammar."""
    if not VALID_RULE_RE.match(rule):
        raise ValueError(
            f"invalid recurrence rule: {rule!r}. "
            "Must be one of: daily | weekly:DOW[,...] | "
            "monthly:N | monthly:last | yearly:MM-DD | every_n_days:N"
        )
    # Extra semantic checks
    m = _RE_WEEKLY.match(rule)
    if m:
        for token in m.group("days").upper().split(","):
            if token not in _WEEKDAY_MAP:
                raise ValueError(
                    f"unknown weekday token {token!r} in rule {rule!r}"
                )
    m = _RE_MONTHLY_N.match(rule)
    if m:
        day = int(m.group("day"))
        if not (1 <= day <= 31):
            raise ValueError(
                f"monthly day {day} out of range [1–31] in rule {rule!r}"
            )
    m = _RE_YEARLY.match(rule)
    if m:
        month, day = int(m.group("month")), int(m.group("day"))
        if not (1 <= month <= 12) or not (1 <= day <= 31):
            raise ValueError(
                f"yearly date {month:02d}-{day:02d} out of range in rule {rule!r}"
            )
    m = _RE_EVERY_N.match(rule)
    if m:
        n = int(m.group("n"))
        if n < 1:
            raise ValueError(
                f"every_n_days interval must be ≥ 1, got {n} in rule {rule!r}"
            )
    m = _RE_EVERY_N_HR.match(rule)
    if m:
        n = int(m.group("n"))
        if not (1 <= n <= 168):
            raise ValueError(
                f"every_n_hours interval must be in [1, 168], got {n} in rule {rule!r}"
            )
    m = _RE_EVERY_N_MIN.match(rule)
    if m:
        n = int(m.group("n"))
        if not (1 <= n <= 1440):
            raise ValueError(
                f"every_n_minutes interval must be in [1, 1440], got {n} in rule {rule!r}"
            )


def next_occurrence(rule: str, current_due: datetime) -> datetime:
    """Compute the next fire time after *current_due* for a recurrence rule.

    Rules:
      - "daily"                    — +1 day, same wall time
      - "weekly:MON,WED,FRI"       — next listed weekday at same wall time
      - "monthly:1"                — first of next month at same wall time
      - "monthly:last"             — last day of next month
      - "yearly:MM-DD"             — annual date, same wall time
      - "every_n_days:N"           — +N days

    Handles end-of-month (Jan 31 monthly → Feb 28/29).
    Handles DST transitions by working in the original tz.
    """
    validate_rule(rule)

    tz = _home_tz()

    # Normalise: make sure we have a tz-aware datetime, then convert to local
    # wall time so that "+1 day" means the same clock time tomorrow even
    # across a DST boundary.
    if current_due.tzinfo is None:
        from datetime import UTC
        current_due = current_due.replace(tzinfo=UTC)
    local = current_due.astimezone(tz)

    rule_lower = rule.lower()

    # -----------------------------------------------------------------------
    # daily
    # -----------------------------------------------------------------------
    if rule_lower == "daily":
        return _shift_days_wall(local, 1, tz=tz)

    # -----------------------------------------------------------------------
    # every_n_days:N
    # -----------------------------------------------------------------------
    m = _RE_EVERY_N.match(rule)
    if m:
        n = int(m.group("n"))
        return _shift_days_wall(local, n, tz=tz)

    # -----------------------------------------------------------------------
    # every_n_hours:N — absolute clock arithmetic, not wall-time preserving.
    # -----------------------------------------------------------------------
    m = _RE_EVERY_N_HR.match(rule)
    if m:
        n = int(m.group("n"))
        return (current_due + timedelta(hours=n)).astimezone(tz)

    # -----------------------------------------------------------------------
    # every_n_minutes:N — absolute clock arithmetic, not wall-time preserving.
    # -----------------------------------------------------------------------
    m = _RE_EVERY_N_MIN.match(rule)
    if m:
        n = int(m.group("n"))
        return (current_due + timedelta(minutes=n)).astimezone(tz)

    # -----------------------------------------------------------------------
    # weekly:DOW[,DOW...]
    # -----------------------------------------------------------------------
    m = _RE_WEEKLY.match(rule.upper())
    if m:
        target_isoweekdays = sorted(
            _WEEKDAY_MAP[d] for d in m.group("days").upper().split(",")
        )
        current_iso = local.isoweekday()  # Mon=1 … Sun=7
        # Find the soonest (smallest positive) diff across all listed weekdays.
        # Using % 7 means a diff of 0 maps to 7 (same weekday = next week).
        min_diff = min(
            (dow - current_iso) % 7 or 7  # 0 → 7 so "same day" wraps to +7
            for dow in target_isoweekdays
        )
        return _shift_days_wall(local, min_diff, tz=tz)

    # -----------------------------------------------------------------------
    # monthly:N or monthly:last
    # -----------------------------------------------------------------------
    m_last = _RE_MONTHLY_LAST.match(rule)
    if m_last:
        return _next_monthly_last(local, tz=tz)

    m_n = _RE_MONTHLY_N.match(rule)
    if m_n:
        target_day = int(m_n.group("day"))
        return _next_monthly_day(local, target_day, tz=tz)

    # -----------------------------------------------------------------------
    # yearly:MM-DD
    # -----------------------------------------------------------------------
    m = _RE_YEARLY.match(rule)
    if m:
        target_month = int(m.group("month"))
        target_day = int(m.group("day"))
        return _next_yearly(local, target_month, target_day, tz=tz)

    # Should be unreachable — validate_rule would have caught it.
    raise ValueError(f"unhandled recurrence rule: {rule!r}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _shift_days_wall(dt: datetime, days: int, tz: ZoneInfo) -> datetime:
    """Add *days* calendar days while preserving the wall clock time.

    We construct the shifted date in local tz via timedelta, then
    re-attach the same H:M:S:μs in the target tz — this correctly
    handles DST gaps/folds by using ``fold=0`` (prefer first occurrence).
    """
    shifted = dt + timedelta(days=days)
    return shifted.astimezone(tz)


def _replace_day_preserve_wall(dt: datetime, day: int, tz: ZoneInfo) -> datetime:
    """Return the same month/year/H:M:S but with *day* replaced."""
    # Advance to the next month if day > month length.
    year, month = dt.year, dt.month
    max_day = calendar.monthrange(year, month)[1]
    if day > max_day:
        # Roll to next month, clamped.
        month += 1
        if month > 12:
            month = 1
            year += 1
        max_day = calendar.monthrange(year, month)[1]
        day = min(day, max_day)
    return dt.replace(year=year, month=month, day=day).astimezone(tz)


def _next_monthly_day(dt: datetime, target_day: int, tz: ZoneInfo) -> datetime:
    """First occurrence of *target_day* in a future month (wraps year if needed)."""
    year, month = dt.year, dt.month
    # Always go to next month.
    month += 1
    if month > 12:
        month = 1
        year += 1
    # Clamp target_day to the actual last day of that month.
    max_day = calendar.monthrange(year, month)[1]
    clamped_day = min(target_day, max_day)
    candidate = dt.replace(year=year, month=month, day=clamped_day)
    return candidate.astimezone(tz)


def _next_monthly_last(dt: datetime, tz: ZoneInfo) -> datetime:
    """Last day of the month after *dt*'s month."""
    year, month = dt.year, dt.month
    month += 1
    if month > 12:
        month = 1
        year += 1
    last_day = calendar.monthrange(year, month)[1]
    candidate = dt.replace(year=year, month=month, day=last_day)
    return candidate.astimezone(tz)


def _next_yearly(
    dt: datetime, target_month: int, target_day: int, tz: ZoneInfo
) -> datetime:
    """Next occurrence of *MM-DD* strictly after *dt*."""
    year = dt.year
    max_day = calendar.monthrange(year, target_month)[1]
    clamped = min(target_day, max_day)
    candidate = dt.replace(year=year, month=target_month, day=clamped)
    if candidate <= dt:
        year += 1
        max_day = calendar.monthrange(year, target_month)[1]
        clamped = min(target_day, max_day)
        candidate = dt.replace(year=year, month=target_month, day=clamped)
    return candidate.astimezone(tz)


# ---------------------------------------------------------------------------
# Smoke tests (run with: python -m tools.reminders.recurrence)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import UTC

    base = datetime(2026, 1, 15, 9, 0, 0, tzinfo=UTC)  # Thu Jan 15 09:00 UTC

    cases = [
        ("daily",              base),
        ("every_n_days:14",    base),
        ("every_n_days:123",   base),
        ("weekly:MON,WED,FRI", base),
        ("monthly:1",          base),
        ("monthly:last",       base),
        ("yearly:03-15",       base),
    ]

    print(f"Base: {base.isoformat()}")
    print()
    for rule, dt in cases:
        nxt = next_occurrence(rule, dt)
        print(f"  {rule:<22}  →  {nxt.isoformat()}")

    # Edge cases
    print()
    print("Edge cases:")
    jan31 = datetime(2026, 1, 31, 10, 0, 0, tzinfo=UTC)
    print(f"  monthly:1  from Jan-31  →  {next_occurrence('monthly:1', jan31).isoformat()}")
    print(f"  monthly:last from Jan-31 → {next_occurrence('monthly:last', jan31).isoformat()}")

    sat = datetime(2026, 1, 17, 9, 0, 0, tzinfo=UTC)  # Saturday
    print(f"  weekly:MON,WED,FRI from Sat → {next_occurrence('weekly:MON,WED,FRI', sat).isoformat()}")
