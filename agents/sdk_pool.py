"""Persistent SDK client pool.

Two long-lived ClaudeSDKClient instances:

  live  — full MCP, full hooks, Hikari persona. Used by run_user_turn +
           run_visible_proactive. Cold-started with session resume from DB.
           Self-heals on ProcessError by reconnecting with the latest
           stored session_id.

  judge — Haiku, max_turns=1, no MCP, no hooks, neutral system prompt.
           Used by drift_judge.judge_outbound.

Everything else (run_internal_control, run_reflection_call,
run_isolated_turn, bounded_rewrite) stays ephemeral — different system
prompts or intentionally fresh sessions.

Pool state is module-level so importlib.reload(agents.runtime) doesn't
reset handles.  The module is safe to import at any time; startup() must
be awaited before get_live_client() / get_haiku_judge() are called.
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
_judge: _Handle = _Handle()
_started: bool = False
_startup_lock: asyncio.Lock = asyncio.Lock()
_live_recycle_pending: bool = False
_judge_recycle_pending: bool = False


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
    return _build_options(
        resume=resume,
        max_turns=DEFAULT_MAX_TURNS,
        max_budget_usd=0.50,
        extra_allowed_tools=None,
        inject_memory_enabled=True,
    )


def judge_options() -> ClaudeAgentOptions:
    """ClaudeAgentOptions for the persistent Haiku judge.

    Extracted from drift_judge.judge_outbound so sdk_pool can build the
    same client without duplicating the option values.
    """
    from claude_agent_sdk import ClaudeAgentOptions

    from agents import config as cfg
    from agents.runtime import MODEL_FALLBACK

    rubric = str(cfg.get("drift_telemetry.rubric") or "")
    return ClaudeAgentOptions(
        model=str(cfg.get("drift_telemetry.model", MODEL_FALLBACK)),
        max_turns=1,
        max_budget_usd=float(cfg.get("drift_telemetry.max_budget_usd", 0.01)),
        system_prompt=rubric,
        # No resume, no MCP, no hooks — isolated judging session.
    )


# --------------------------------------------------------------------------- #
# Recycle thresholds                                                           #
# --------------------------------------------------------------------------- #


def _live_recycle_after() -> int:
    return int(cfg.get("runtime.live_client_recycle_after_turns", 500))


def _judge_recycle_after() -> int:
    return int(cfg.get("runtime.judge_client_recycle_after_calls", 100))


# --------------------------------------------------------------------------- #
# Connect / disconnect helpers                                                 #
# --------------------------------------------------------------------------- #


async def _connect_live(resume: str | None) -> ClaudeSDKClient:
    from claude_agent_sdk import ClaudeSDKClient
    options = _build_live_options(resume)
    client: ClaudeSDKClient = ClaudeSDKClient(options=options)
    await client.connect()
    return client


async def _connect_judge() -> ClaudeSDKClient:
    from claude_agent_sdk import ClaudeSDKClient
    client: ClaudeSDKClient = ClaudeSDKClient(options=judge_options())
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
    """Idempotent startup — cold-connects both clients.

    Called once from telegram_bridge post_init.  Safe to call multiple
    times; subsequent calls are no-ops.
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

        try:
            _judge.client = await _connect_judge()
            logger.info("sdk_pool: judge client connected")
        except Exception:
            logger.exception(
                "sdk_pool: judge client failed to connect — drift judging disabled until restart"
            )
            _judge.client = None

        _started = True


async def shutdown() -> None:
    """Idempotent shutdown — disconnects both clients."""
    global _started
    async with _startup_lock:
        if not _started:
            return
        logger.info("sdk_pool: shutting down")
        await _disconnect(_live.client)
        _live.client = None
        await _disconnect(_judge.client)
        _judge.client = None
        _started = False


# --------------------------------------------------------------------------- #
# Reconnect                                                                    #
# --------------------------------------------------------------------------- #


async def _reconnect_live(reason: str) -> None:
    """Reconnect live client under connect_lock."""
    async with _live.connect_lock:
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


async def _reconnect_judge(reason: str) -> None:
    """Reconnect judge client under connect_lock."""
    async with _judge.connect_lock:
        logger.info("sdk_pool: reconnecting judge client (reason=%s)", reason)
        await _disconnect(_judge.client)
        _judge.client = None
        try:
            _judge.client = await _connect_judge()
            _judge.counter = 0
            logger.info("sdk_pool: judge client reconnected")
        except Exception:
            logger.exception("sdk_pool: judge client reconnect failed")
            _judge.client = None
            raise


# --------------------------------------------------------------------------- #
# Public accessors                                                             #
# --------------------------------------------------------------------------- #


async def get_live_client() -> ClaudeSDKClient:
    """Return the live client, reconnecting if dead or over recycle threshold."""
    if _live.client is None:
        await _reconnect_live("client is None")
    assert _live.client is not None, "sdk_pool: live client unavailable after reconnect attempt"
    return _live.client


async def get_haiku_judge() -> ClaudeSDKClient:
    """Return the judge client, reconnecting if dead or over recycle threshold."""
    if _judge.client is None:
        await _reconnect_judge("client is None")
    assert _judge.client is not None, "sdk_pool: judge client unavailable after reconnect attempt"
    return _judge.client


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


def _maybe_schedule_judge_recycle() -> None:
    """Increment call counter; schedule recycle if threshold exceeded.

    The pending flag ensures only one recycle task is in-flight at a time.
    """
    global _judge_recycle_pending
    _judge.counter += 1
    threshold = _judge_recycle_after()
    if _judge.counter >= threshold and not _judge_recycle_pending:
        _judge_recycle_pending = True
        logger.info(
            "sdk_pool: scheduling judge recycle after %d calls",
            threshold,
        )

        async def _recycle_and_clear():
            global _judge_recycle_pending
            try:
                await _reconnect_judge(f"recycle after {threshold} calls")
            finally:
                _judge_recycle_pending = False

        asyncio.create_task(_recycle_and_clear())
