"""``reminder_cancel`` — stop a reminder by id (idempotent)."""
from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from storage import db
from tools._response import ok as _ok


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
