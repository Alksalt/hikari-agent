"""``receipt_delete`` — delete an entry by its numeric id."""
from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from tools._response import ok as _ok
from tools.day_receipt import _db as _receipt_db


@tool(
    "receipt_delete",
    (
        "Delete a single entry by its numeric id. Use when the user "
        "says 'remove that' or 'undo the last log'. "
        "entry_id: required, the id returned by receipt_add / "
        "receipt_today / receipt_get / receipt_search."
    ),
    {"entry_id": int},
)
async def receipt_delete(args: dict[str, Any]) -> dict[str, Any]:
    raw_id = args.get("entry_id")
    if raw_id is None:
        return _ok("refused: receipt_delete needs entry_id")
    try:
        entry_id = int(raw_id)
    except (TypeError, ValueError):
        return _ok(f"refused: invalid entry_id {raw_id!r}")
    deleted = _receipt_db.delete_entry(entry_id)
    if deleted:
        return _ok(f"deleted entry #{entry_id}", data={"ok": True, "id": entry_id})
    return _ok(f"no entry with id={entry_id}", data={"ok": False, "id": entry_id})
