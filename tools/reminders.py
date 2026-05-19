"""Phase 10: reminders MCP tools.

reminder_create: schedule a future poke. Optionally sync to Google Calendar
(via the drive_gmail subagent, asynchronously — the background scheduler job
drains the sync queue, so this tool returns immediately).
reminder_list: show active (or all) reminders.
reminder_cancel: stop a reminder.
reminder_snooze: push fire time forward by N minutes.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from claude_agent_sdk import tool

from storage import db

logger = logging.getLogger(__name__)


def _ok(text: str, data: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if data is not None:
        body["data"] = data
    return body


def _parse_iso(s: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=UTC)
        return d
    except (ValueError, TypeError):
        return None


_VALID_REPEAT = {None, "", "daily", "weekly", "monthly", "yearly"}


@tool(
    "reminder_create",
    "Schedule a reminder. when_iso is an ISO-8601 timestamp (UTC or with tz). "
    "text is the message Hikari will send when it fires. lead_minutes (default 0) "
    "fires the reminder N minutes BEFORE when_iso — useful for events ('remind me "
    "1h before my 14:00 meeting' -> when_iso=14:00, lead_minutes=60, fires at 13:00). "
    "repeat one of {daily, weekly, monthly, yearly} for simple repeats, or an "
    "RRULE string for advanced. sync_to_gcal=True queues a Google Calendar mirror "
    "(non-blocking — the GCal sync job drains the queue separately).",
    {"when_iso": str, "text": str, "lead_minutes": int, "repeat": str,
     "sync_to_gcal": bool},
)
async def reminder_create(args: dict[str, Any]) -> dict[str, Any]:
    when_iso = (args.get("when_iso") or "").strip()
    text = (args.get("text") or "").strip()
    lead_minutes = int(args.get("lead_minutes") or 0)
    repeat = (args.get("repeat") or "").strip() or None
    sync_to_gcal = bool(args.get("sync_to_gcal", True))

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

    rid = db.reminder_insert(
        fire_at=when.isoformat(),
        text=text,
        lead_minutes=lead_minutes,
        repeat=repeat,
        gcal_sync_pending=sync_to_gcal,
    )
    return _ok(
        f"reminder #{rid} set for {when.isoformat()} "
        f"(lead {lead_minutes}m, repeat {repeat or 'none'}, "
        f"gcal_sync {'queued' if sync_to_gcal else 'skipped'})",
        data={"id": rid},
    )


@tool(
    "reminder_list",
    "List reminders. active_only=True (default) returns only those still pending. "
    "Returns id, fire_at, text, lead_minutes, repeat, status.",
    {"active_only": bool},
)
async def reminder_list(args: dict[str, Any]) -> dict[str, Any]:
    active_only = bool(args.get("active_only", True))
    rows = db.reminder_list(active_only=active_only)
    if not rows:
        return _ok("no reminders", data={"reminders": []})
    lines = ["reminders:"]
    for r in rows:
        lines.append(
            f"  #{r['id']} {r['fire_at']} — {r['text']} "
            f"(lead {r['lead_minutes']}m, repeat {r['repeat'] or 'none'}, "
            f"status {r['status']})"
        )
    return _ok("\n".join(lines), data={"reminders": rows})


@tool(
    "reminder_cancel",
    "Cancel a reminder by id. Idempotent — cancelling an already-fired or "
    "already-cancelled reminder is a no-op.",
    {"reminder_id": int},
)
async def reminder_cancel(args: dict[str, Any]) -> dict[str, Any]:
    rid = int(args.get("reminder_id") or 0)
    if rid <= 0:
        return _ok("refused: missing reminder_id")
    row = db.reminder_get(rid)
    if row is None:
        return _ok(f"reminder #{rid} not found")
    db.reminder_cancel(rid)
    return _ok(f"reminder #{rid} cancelled")


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
    from storage.db import _conn
    with _conn() as conn:
        conn.execute(
            "UPDATE reminders SET fire_at = ? WHERE id = ?",
            (new_when.isoformat(), rid),
        )
    return _ok(f"reminder #{rid} snoozed to {new_when.isoformat()}")


ALL_TOOLS = [reminder_create, reminder_list, reminder_cancel, reminder_snooze]
