"""Public/gated dispatch tool — ``dispatch_claude_session``.

Spawns a long-running Claude Code session as a nested ClaudeSDKClient
inside Hikari's process. Default ``allowed_tools`` is read-only; passing
Edit/Write/Bash triggers the PreToolUse arg-gate, which defers the call
until the owner types CONFIRM-SEND. After approval the resume codepath
invokes the sibling ``dispatch_claude_session_confirmed`` (see
``session_confirmed.py``) which carries the requested allowlist verbatim
and skips the gate.
"""
from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from tools.dispatch._shared import _do_dispatch, WORK_DIR_ROOT


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
)
async def dispatch_claude_session(args: dict[str, Any]) -> dict[str, Any]:
    return await _do_dispatch(args)
