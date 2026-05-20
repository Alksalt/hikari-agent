"""task_create — track a fuzzy open loop with no real clock."""
from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from storage import db
from tools._response import ok as _ok


@tool(
    "task_create",
    "Track a FUZZY open loop with NO real clock — something to follow up on "
    "'later' or 'next time we talk'. Open tasks are injected into context so Hikari "
    "remembers what she owes. e.g. user says 'don't let me forget to ask my mom "
    "about the recipe sometime' → task_create. "
    "Don't use this for time-bound reminders ('in 30 min', 'tomorrow at 9') — use "
    "`reminder_create` so a real push fires. Don't use this for a fact (use `remember`).",
    {"subject": str, "description": str, "due_at": str},
)
async def task_create(args: dict[str, Any]) -> dict[str, Any]:
    subject = (args.get("subject") or "").strip()
    if not subject:
        return _ok("task_create: subject is required.")
    description = (args.get("description") or "").strip() or None
    due_at = (args.get("due_at") or "").strip() or None
    task_id = db.create_task(subject, description, due_at)
    return _ok(f"task #{task_id} created: {subject}")
