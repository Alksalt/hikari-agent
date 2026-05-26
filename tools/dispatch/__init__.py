"""dispatch — spawn a long-running Claude Code session as a nested
ClaudeSDKClient inside Hikari's process.

DEDICATED MCP SERVER. ``agents/runtime.py`` does
``from tools import dispatch as dispatch_tools`` and registers
``PUBLIC_TOOLS`` against the always-on ``hikari_dispatch`` server.

The shared registry skips ``dispatch`` on purpose (see
``tools/_registry._DEDICATED_SERVER_MODULES``).

Architecture choice: a NESTED SDK client (not a CLI subprocess). Same
OAuth token, native message types, trivial cost extraction. Trade-off:
dies if Hikari restarts — recovered via session_id resume.

Re-exports:
  * ``WORK_DIR_ROOT`` — module constant.
  * ``DISPATCH_EVENTS`` — the queue ``agents/background_listener``
    drains.
  * ``set_owner_chat_id`` — called once by ``telegram_bridge`` post_init.
  * The tool callable itself so ``tests/test_smoke.py`` can
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
    _BG_TASKS,
    _build_dispatch_options,
    _do_dispatch,
    _emit,
    _filter_allowed_tools,
    _owner_chat_id,
    _REQUIRES_EXPLICIT_OWNER_FLAG,
    _run_session,
    _SAFE_DISPATCH_TOOLS,
    _validate_repo,
    set_owner_chat_id,
)
from tools.dispatch.session import dispatch_claude_session

# Public tools — registered on the always-on `hikari_dispatch` MCP server.
PUBLIC_TOOLS = [dispatch_claude_session]

# Backwards-compat alias.
ALL_TOOLS = PUBLIC_TOOLS
