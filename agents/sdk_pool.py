"""Persistent SDK client pool.

One long-lived ClaudeSDKClient instance:

  live  — full MCP, full hooks, Hikari persona. Used by run_user_turn +
           run_visible_proactive. Cold-started with session resume from DB.
           Self-heals on ProcessError by reconnecting with the latest
           stored session_id.

Everything else (run_internal_control, run_reflection_call,
run_isolated_turn, bounded_rewrite, drift judging) stays ephemeral —
different system prompts, intentionally fresh sessions, or the httpx
OpenRouter path (drift_judge uses _call_aux_llm, not this pool).

Pool state is module-level so importlib.reload(agents.runtime) doesn't
reset handles.  The module is safe to import at any time; startup() must
be awaited before get_live_client() is called.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from agents import config as cfg

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Internal handle type                                                         #
# --------------------------------------------------------------------------- #


@dataclass
class _Handle:
    client: ClaudeSDKClient | None = None
    connect_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    counter: int = 0          # turn_count (live) or call_count (judge)


# --------------------------------------------------------------------------- #
# Module-level state — survives importlib.reload(agents.runtime)              #
# --------------------------------------------------------------------------- #

_live: _Handle = _Handle()
_started: bool = False
_startup_lock: asyncio.Lock = asyncio.Lock()
_live_recycle_pending: bool = False
_autonomous_window: bool = False


# --------------------------------------------------------------------------- #
# Autonomous window                                                             #
# --------------------------------------------------------------------------- #


def set_autonomous_window(on: bool) -> None:
    """Mark that the current turn is an autonomous scheduled-action window.

    Called by run_scheduled_action inside ``_RUN_LOCK`` so that gatekeeper can
    bypass CONFIRM-SEND for whitelisted writes without a ContextVar that might
    propagate across asyncio tasks.
    """
    global _autonomous_window
    _autonomous_window = bool(on)


def in_autonomous_window() -> bool:
    """True while run_scheduled_action holds ``_RUN_LOCK``."""
    return _autonomous_window


# --------------------------------------------------------------------------- #
# Feature flag                                                                 #
# --------------------------------------------------------------------------- #


def is_live_persistent_path_enabled() -> bool:
    """Read from cfg (default True). Set runtime.live_client_persistent=false
    to fall back to the ephemeral path for the live client."""
    return bool(cfg.get("runtime.live_client_persistent", True))


# --------------------------------------------------------------------------- #
# Options helpers                                                              #
# --------------------------------------------------------------------------- #


def _build_live_options(resume: str | None) -> ClaudeAgentOptions:
    """Build ClaudeAgentOptions for the persistent live client.

    Named shim — delegates to agents.runtime._build_options so the two
    paths share exactly the same option set.  Imported lazily to avoid
    circular import at module level.
    """
    from agents.runtime import (
        DEFAULT_MAX_TURNS,
        _build_options,  # type: ignore[attr-defined]
    )
    # max_budget_usd intentionally None: SDK enforces this cap per CLI-subprocess
    # lifetime, not per turn. A persistent long-lived client would exhaust any
    # finite cap within a few real turns and then silently fail every subsequent
    # request with error_max_budget_usd (zero-token responses). Cost is bounded
    # by the OAuth Max subscription + max_turns + the 500-turn recycle.
    return _build_options(
        resume=resume,
        max_turns=DEFAULT_MAX_TURNS,
        max_budget_usd=None,
        extra_allowed_tools=None,
        inject_memory_enabled=True,
    )


# --------------------------------------------------------------------------- #
# Recycle thresholds                                                           #
# --------------------------------------------------------------------------- #


def _live_recycle_after() -> int:
    return int(cfg.get("runtime.live_client_recycle_after_turns", 500))


# --------------------------------------------------------------------------- #
# Connect / disconnect helpers                                                 #
# --------------------------------------------------------------------------- #


async def _connect_live(resume: str | None) -> ClaudeSDKClient:
    from claude_agent_sdk import ClaudeSDKClient
    options = _build_live_options(resume)
    client: ClaudeSDKClient = ClaudeSDKClient(options=options)
    await client.connect()
    return client


async def _disconnect(client: ClaudeSDKClient | None) -> None:
    if client is None:
        return
    try:
        await client.disconnect()
    except Exception:  # noqa: BLE001
        logger.debug("sdk_pool: disconnect swallowed exception", exc_info=True)


# --------------------------------------------------------------------------- #
# Startup / shutdown                                                           #
# --------------------------------------------------------------------------- #


async def startup() -> None:
    """Idempotent startup — cold-connects the live client.

    Called once from telegram_bridge post_init.  Safe to call multiple
    times; subsequent calls are no-ops.

    Drift judging is handled by ``agents.runtime._call_aux_llm`` (httpx →
    OpenRouter) and does not need a persistent client here.
    """
    global _started
    async with _startup_lock:
        if _started:
            return

        from storage import db
        resume = db.get_session_id() or None
        logger.info("sdk_pool: starting live client (resume=%s)", "present" if resume else "none")

        try:
            _live.client = await _connect_live(resume)
            logger.info("sdk_pool: live client connected")
        except Exception:
            logger.exception(
                "sdk_pool: live client failed to connect — persistent path disabled until restart"
            )
            _live.client = None

        _started = True


async def shutdown() -> None:
    """Idempotent shutdown — disconnects the live client."""
    global _started
    async with _startup_lock:
        if not _started:
            return
        logger.info("sdk_pool: shutting down")
        await _disconnect(_live.client)
        _live.client = None
        _started = False


# --------------------------------------------------------------------------- #
# Reconnect                                                                    #
# --------------------------------------------------------------------------- #


async def _do_reconnect_live(reason: str) -> None:
    """Inner reconnect body — must be called while connect_lock is held."""
    logger.info("sdk_pool: reconnecting live client (reason=%s)", reason)
    await _disconnect(_live.client)
    _live.client = None

    from storage import db
    resume = db.get_session_id() or None
    try:
        _live.client = await _connect_live(resume)
        _live.counter = 0
        logger.info(
            "sdk_pool: live client reconnected (resume=%s)", "present" if resume else "none"
        )
    except Exception:
        logger.exception("sdk_pool: live client reconnect failed")
        _live.client = None
        raise


async def _reconnect_live(reason: str, *, lock_run: bool = True) -> None:
    """Reconnect live client under connect_lock.

    lock_run=True (default): also acquires _RUN_LOCK before connect_lock so no
    user turn can interleave with the reconnect. Pass lock_run=False from callers
    that are already holding _RUN_LOCK (avoids deadlock).
    """
    if lock_run:
        from agents.runtime import _RUN_LOCK  # late import to break cycle
        async with _RUN_LOCK:
            async with _live.connect_lock:
                await _do_reconnect_live(reason)
    else:
        async with _live.connect_lock:
            await _do_reconnect_live(reason)


# --------------------------------------------------------------------------- #
# Public accessors                                                             #
# --------------------------------------------------------------------------- #


async def get_live_client() -> ClaudeSDKClient:
    """Return the live client, reconnecting if dead or over recycle threshold."""
    if _live.client is None:
        await _reconnect_live("client is None", lock_run=False)
    assert _live.client is not None, "sdk_pool: live client unavailable after reconnect attempt"
    return _live.client


# --------------------------------------------------------------------------- #
# Recycle scheduling (called between _RUN_LOCK acquisitions)                  #
# --------------------------------------------------------------------------- #


def _maybe_schedule_live_recycle() -> None:
    """Increment turn counter; schedule recycle if threshold exceeded.

    Must be called OUTSIDE _RUN_LOCK so the recycle happens at the next
    idle window.  Called by _invoke_sdk_persistent_live after a turn
    completes.

    The pending flag ensures only one recycle task is in-flight at a time —
    without it, every subsequent call past the threshold would spawn another
    reconnect subprocess.
    """
    global _live_recycle_pending
    _live.counter += 1
    threshold = _live_recycle_after()
    if _live.counter >= threshold and not _live_recycle_pending:
        _live_recycle_pending = True
        logger.info(
            "sdk_pool: scheduling live recycle after %d turns",
            threshold,
        )

        async def _recycle_and_clear():
            global _live_recycle_pending
            try:
                await _reconnect_live(f"recycle after {threshold} turns")
            finally:
                _live_recycle_pending = False

        asyncio.create_task(_recycle_and_clear())


