"""``reminder_snooze`` — push fire time forward by N minutes."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from claude_agent_sdk import tool

from storage import db
from tools._response import ok as _ok
from tools.reminders._shared import _parse_iso


@tool(
    "reminder_snooze",
    "Push a reminder's fire time forward by N minutes. Useful when the user says "
    "'remind me in 30 min instead'.",
    {"reminder_id": int, "by_minutes": int},
)
async def reminder_snooze(args: dict[str, Any]) -> dict[str, Any]:
    rid = int(args.get("reminder_id") or 0)
    by_minutes = int(args.get("by_minutes") or 0)
    if rid <= 0:
        return _ok("refused: missing reminder_id")
    if by_minutes <= 0:
        return _ok("refused: by_minutes must be positive")
    row = db.reminder_get(rid)
    if row is None or row["status"] != "active":
        return _ok(f"refused: reminder #{rid} is not active")
    when = _parse_iso(row["fire_at"])
    if when is None:
        return _ok(f"refused: reminder #{rid} has malformed fire_at")
    new_when = when + timedelta(minutes=by_minutes)
    db.reminder_update_fire_at(rid, new_when.isoformat())
    db.reminder_requeue_sync(rid)
    return _ok(f"reminder #{rid} snoozed to {new_when.isoformat()}")
