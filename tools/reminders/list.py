"""``reminder_list`` — show active (or all) reminders."""
from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from storage import db
from tools._annotations import annotations_for
from tools._response import ok as _ok


@tool(
    "reminder_list",
    "List reminders. active_only=True (default) returns only those still pending. "
    "Returns id, fire_at, text, lead_minutes, repeat, status.",
    {"active_only": bool},
    annotations=annotations_for("reminder_list"),
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
