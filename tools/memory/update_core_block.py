"""update_core_block — overwrite an always-on labeled memory block."""
from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from storage import db
from tools._response import ok as _ok


@tool(
    "update_core_block",
    "Overwrite a labeled always-on memory block injected on every turn (e.g. "
    "'user_profile', 'mood_today', 'preoccupation'). Used sparingly — these are "
    "load-bearing system context. e.g. user explicitly redefines a stable trait: "
    "'actually my pronouns are they/them now' → update_core_block('user_profile', …). "
    "Don't use this for one-off facts (use `remember`) or for transient open loops "
    "(use `task_create`).",
    {"label": str, "content": str},
)
async def update_core_block(args: dict[str, Any]) -> dict[str, Any]:
    label = (args.get("label") or "").strip()
    content = (args.get("content") or "").strip()
    if not label:
        return _ok("update_core_block: label is required.")
    db.upsert_core_block(label, content)
    return _ok(f"core block {label!r} updated ({len(content)} chars).")
