"""Post-approval execution variant — ``dispatch_claude_session_confirmed``.

NOT in Hikari's default allowed_tools; only injected per-turn by the
defer-resume codepath in ``tools/approvals._resume_after_defer``. Lives
on a separate ``hikari_dispatch_confirmed`` MCP server attached only
during the resume turn (see ``agents/runtime._dispatch_confirmed_server``)
so its schema isn't in the manifest otherwise, and so it bypasses the
PreToolUse arg-gate that would otherwise re-defer it.

The body is identical to the public variant; the only difference is
which server it's wired into and whether the gate fires.
"""
from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from tools.dispatch._shared import _do_dispatch


@tool(
    "dispatch_claude_session_confirmed",
    "POST-APPROVAL execution path for dispatch_claude_session — performs the "
    "actual dispatch WITHOUT going through the approval gate. NOT in Hikari's "
    "default allowed_tools; only injected per-turn by the defer-resume codepath "
    "in tools/approvals._resume_after_defer. If you (the lead agent) see this "
    "tool in your allowlist, you were invoked via the resume path; call it once "
    "with the args from the system prompt and stop.",
    {"repo_path": str, "task": str, "allowed_tools": str, "max_turns": int},
)
async def dispatch_claude_session_confirmed(args: dict[str, Any]) -> dict[str, Any]:
    return await _do_dispatch(args)
