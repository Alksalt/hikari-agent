"""Public/gated dispatch tool — ``dispatch_claude_session``.

Spawns a long-running Claude Code session as a nested ClaudeSDKClient
inside Hikari's process. Default ``allowed_tools`` is read-only; passing
Edit/Write/Bash triggers the gatekeeper can_use_tool gate, which pauses
the call until the owner types CONFIRM-SEND via Telegram.
"""
from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from tools._annotations import annotations_for
from tools.dispatch._shared import WORK_DIR_ROOT, _do_dispatch


@tool(
    "dispatch_claude_session",
    "Spawn a background CODE-MODIFICATION / repo-investigation session in a SEPARATE "
    "repo on the user's Mac Mini. NOT a way to answer a chat question — this fires "
    "an autonomous Claude Code worker on a codebase and the user gets progress + "
    "final via Telegram async (minutes, not seconds). Default allowed_tools is "
    "read-only; adding Edit/Write/Bash triggers a CONFIRM-SEND owner gate. "
    f"repo_path must be absolute and under {WORK_DIR_ROOT}/. "
    "e.g. user says 'go look at the meria repo and patch the auth bug' → dispatch. "
    "Don't use this to answer a question with public-web info (use `research`) or "
    "to look up something in the user's notes (use `wiki_search`).",
    {"repo_path": str, "task": str, "allowed_tools": str, "max_turns": int},
    annotations=annotations_for("dispatch_claude_session"),
)
async def dispatch_claude_session(args: dict[str, Any]) -> dict[str, Any]:
    return await _do_dispatch(args)
