"""``reminder_create`` — schedule a future poke.

Optionally syncs to Google Calendar / Apple Reminders. The sync work
is queued in the DB (``gcal_sync_pending`` / ``apple_sync_pending``)
and drained asynchronously by the background scheduler — this tool
returns immediately.
"""
from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from claude_agent_sdk import tool

from storage import db
from tools._annotations import annotations_for
from tools._response import ok as _ok
from tools.reminders._shared import _VALID_REPEAT, _parse_iso
from tools.reminders.recurrence import validate_rule as _validate_recurrence_rule

logger = logging.getLogger(__name__)


@tool(
    "reminder_create",
    (
        "Schedule a reminder that fires as a real Telegram push at when_iso. "
        "when_iso MUST be a fully-resolved ISO-8601 timestamp (UTC or with tz "
        "offset) — the parser refuses anything else. If the user gives a "
        "relative time, YOU compute the ISO from the `# now` block injected "
        "at the top of your context. Do not call this tool with natural-"
        "language time strings like 'in 1h' or 'tomorrow'. "
        "Examples: "
        "(EN) user 'remind me in 5 min to stretch', `# now` utc 2026-05-20T18:00:00+00:00 "
        "→ when_iso='2026-05-20T18:05:00+00:00', text='stretch'. "
        "(UK) user 'нагадай через годину написати маріку', `# now` utc 2026-05-20T18:00:00+00:00 "
        "→ when_iso='2026-05-20T19:00:00+00:00', text='написати маріку'. "
        "(RU) user 'напомни мне завтра в 9 позвонить маме', "
        "`# now` local 2026-05-20 18:00 Europe/Kyiv → "
        "when_iso='2026-05-21T09:00:00+03:00', text='позвонить маме'. "
        "text is what Hikari will say when the reminder fires. "
        "lead_minutes (default 0) fires N minutes BEFORE when_iso — useful for "
        "events ('remind me 1h before my 14:00 meeting' → when_iso=14:00, "
        "lead_minutes=60, fires at 13:00). "
        "repeat one of {daily, weekly, monthly, yearly} for simple repeats, or "
        "an RRULE string for advanced. "
        "recurrence: structured recurrence rule for smart rescheduling. "
        "Grammar: 'daily' | 'weekly:MON,WED,FRI' | 'monthly:1' | 'monthly:last' "
        "| 'yearly:MM-DD' | 'every_n_days:N'. "
        "When set the reminder auto-reschedules after each fire — it keeps "
        "looping until the user explicitly cancels it. "
        "sync_to_gcal=True queues a Google Calendar mirror (non-blocking — the "
        "GCal sync job drains the queue separately). "
        "sync_to_apple=True queues an Apple Reminders mirror (macOS only, "
        "non-blocking)."
    ),
    {"when_iso": str, "text": str, "lead_minutes": int, "repeat": str,
     "recurrence": str, "sync_to_gcal": bool, "sync_to_apple": bool},
    annotations=annotations_for("reminder_create"),
)
async def reminder_create(args: dict[str, Any]) -> dict[str, Any]:
    when_iso = (args.get("when_iso") or "").strip()
    text = (args.get("text") or "").strip()
    lead_minutes = int(args.get("lead_minutes") or 0)
    repeat = (args.get("repeat") or "").strip() or None
    recurrence = (args.get("recurrence") or "").strip() or None
    sync_to_gcal = bool(args.get("sync_to_gcal", True))
    # Default True on macOS; False elsewhere (EventKit is Apple-only).
    sync_to_apple = bool(args.get("sync_to_apple", sys.platform == "darwin"))

    if not text:
        return _ok("refused: empty text")
    when = _parse_iso(when_iso)
    if when is None:
        return _ok(f"refused: cannot parse when_iso={when_iso!r}")
    if when - timedelta(minutes=lead_minutes) <= datetime.now(UTC):
        return _ok("refused: fire time is in the past")
    if repeat not in _VALID_REPEAT and not repeat.upper().startswith("RRULE:"):
        return _ok(
            f"refused: repeat={repeat!r} must be one of {{daily,weekly,monthly,yearly}} "
            f"or an RRULE string"
        )
    if recurrence is not None:
        try:
            _validate_recurrence_rule(recurrence)
        except ValueError as exc:
            return _ok(f"refused: {exc}")

    rid = db.reminder_insert(
        fire_at=when.isoformat(),
        text=text,
        lead_minutes=lead_minutes,
        repeat=repeat,
        recurrence_rule=recurrence,
        gcal_sync_pending=sync_to_gcal,
        apple_sync_pending=sync_to_apple,
    )
    return _ok(
        f"reminder #{rid} set for {when.isoformat()} "
        f"(lead {lead_minutes}m, repeat {repeat or 'none'}, "
        f"recurrence {recurrence or 'none'}, "
        f"gcal_sync {'queued' if sync_to_gcal else 'skipped'}, "
        f"apple_sync {'queued' if sync_to_apple else 'skipped'})",
        data={"id": rid},
    )
