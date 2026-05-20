"""task_update — change the status of an existing open-loop task."""
from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from storage import db
from tools._response import ok as _ok


@tool(
    "task_update",
    "Update an existing open-loop task by id. Valid statuses: pending, in_progress, "
    "completed, dropped. e.g. user just answered a follow-up you tracked earlier → "
    "task_update(id, 'completed'). Don't use this to create new tasks (use `task_create`) "
    "or to invalidate a fact (use `mark_fact_invalid`).",
    {"task_id": int, "status": str},
)
async def task_update(args: dict[str, Any]) -> dict[str, Any]:
    task_id = int(args.get("task_id") or 0)
    status = (args.get("status") or "").strip().lower()
    if not task_id or status not in ("pending", "in_progress", "completed", "dropped"):
        return _ok("task_update: task_id and a valid status are required.")
    db.update_task(task_id, status=status)
    return _ok(f"task #{task_id} -> {status}")
