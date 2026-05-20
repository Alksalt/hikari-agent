"""Shared helpers for the reminder tools.

``_parse_iso`` is the single ISO-8601 parser all four tools use to
read ``when_iso`` (create) or ``fire_at`` (snooze) timestamps. It is
deliberately strict: it accepts only what ``datetime.fromisoformat``
accepts and defaults naive timestamps to UTC. The natural-language
parsing happens in the model, not here — see ``reminder_create``'s
description, which tells the model to compute an ISO from the
``# now`` block injected at the top of its context.

``_VALID_REPEAT`` enumerates the simple repeat keywords; anything
starting with ``RRULE:`` is also accepted (free-form advanced
schedules are validated downstream by the scheduler).
"""
from __future__ import annotations

from datetime import UTC, datetime


def _parse_iso(s: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=UTC)
        return d
    except (ValueError, TypeError):
        return None


_VALID_REPEAT = {None, "", "daily", "weekly", "monthly", "yearly"}
