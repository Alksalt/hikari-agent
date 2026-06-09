"""``reminder_list`` — show active (pending) reminders, soonest first.

Returns id, text, next fire time in local tz, recurrence if any.
``include_done=False`` (default) returns only pending reminders; set
True to include already-fired and cancelled ones.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from claude_agent_sdk import tool

from storage import db
from tools._annotations import annotations_for
from tools._response import ok as _ok


def _fire_at_local(fire_at_iso: str) -> str:
    """Convert a stored ISO timestamp to local tz string, best-effort."""
    try:
        from zoneinfo import ZoneInfo
        tz_name = os.environ.get("HOME_TZ") or "UTC"
        dt = datetime.fromisoformat(fire_at_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        local = dt.astimezone(ZoneInfo(tz_name))
        return local.strftime(f"%Y-%m-%d %H:%M {tz_name}")
    except Exception:
        return fire_at_iso[:16]


@tool(
    "reminder_list",
    "List active (pending) reminders soonest first. "
    "Returns id, text, next fire time in local tz, recurrence if any. "
    "include_done=False (default) returns only pending reminders; set True "
    "to also include fired/cancelled ones.",
    {"include_done": bool},
    annotations=annotations_for("reminder_list"),
)
async def reminder_list(args: dict[str, Any]) -> dict[str, Any]:
    include_done = bool(args.get("include_done", False))
    # active_only is the inverse of include_done
    rows = db.reminder_list(active_only=not include_done)
    if not rows:
        label = "reminders" if include_done else "active reminders"
        return _ok(f"no {label}", data={"reminders": []})
    lines = ["reminders:"]
    for r in rows:
        fire_str = _fire_at_local(r.get("fire_at") or "")
        recur = r.get("recurrence_rule") or r.get("repeat") or None
        recur_str = f", recurs: {recur}" if recur else ""
        status_str = (
            f" [{r['status']}]" if r.get("status") != "active" else ""
        )
        lines.append(
            f"  #{r['id']} {fire_str} — {r['text']}{recur_str}{status_str}"
        )
    return _ok("\n".join(lines), data={"reminders": rows})
