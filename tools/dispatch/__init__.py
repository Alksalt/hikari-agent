"""dispatch — spawn a long-running Claude Code session as a nested
ClaudeSDKClient inside Hikari's process.

DEDICATED MCP SERVER (two of them, actually). ``agents/runtime.py`` does
``from tools import dispatch as dispatch_tools`` and registers:
  * ``PUBLIC_TOOLS`` against the always-on ``hikari_dispatch`` server,
  * ``CONFIRMED_TOOLS`` against a conditional ``hikari_dispatch_confirmed``
    server attached only during a defer-resume turn.

The shared registry skips ``dispatch`` on purpose (see
``tools/_registry._DEDICATED_SERVER_MODULES``).

Architecture choice: a NESTED SDK client (not a CLI subprocess). Same
OAuth token, native message types, trivial cost extraction. Trade-off:
dies if Hikari restarts — recovered via session_id resume.

Re-exports:
  * ``WORK_DIR_ROOT`` — module constant; some external callers
    (``agents/subagents.py``) import it from this package.
  * ``DISPATCH_EVENTS`` — the queue ``agents/background_listener``
    drains.
  * ``set_owner_chat_id`` — called once by ``telegram_bridge`` post_init.
  * The two tool callables themselves so ``tests/test_smoke.py`` can
    invoke ``dispatch.dispatch_claude_session.handler`` directly after
    ``importlib.reload(dispatch)``.
"""
from __future__ import annotations

from tools.dispatch._shared import (  # noqa: F401 — back-compat re-exports
    DEFAULT_ALLOWED_TOOLS,
    DEFAULT_BUDGET_USD,
    DEFAULT_MAX_TURNS,
    DISPATCH_EVENTS,
    WORK_DIR_ROOT,
    _build_dispatch_options,
    _do_dispatch,
    _emit,
    _owner_chat_id,
    _run_session,
    _validate_repo,
    set_owner_chat_id,
)
from tools.dispatch.session import dispatch_claude_session
from tools.dispatch.session_confirmed import dispatch_claude_session_confirmed

# Public tools — registered on the always-on `hikari_dispatch` MCP server.
PUBLIC_TOOLS = [dispatch_claude_session]

# Phase 8: confirmed-sibling for the dispatch arg-gate. Lives on a separate
# `hikari_dispatch_confirmed` MCP server attached only during the resume turn
# (see agents/runtime._dispatch_confirmed_server).
CONFIRMED_TOOLS = [dispatch_claude_session_confirmed]

# Backwards-compat alias.
ALL_TOOLS = PUBLIC_TOOLS
