"""``progress`` — in-turn progress feedback tool.

Two modes
---------
typing  Send a Telegram ``sendChatAction("typing")`` indicator.
        Fast, ephemeral, does not count toward the text-emission cap.

text    Send a real Telegram message with the progress text.
        Counts toward the rate-limit cap (max 4 per turn, min 1.5 s gap).

Auto-detect (default)
---------------------
If ``mode`` is omitted or ``"auto"``:
  - message shorter than 60 chars AND no ``surprise=true`` in args → typing
  - otherwise → text

Rate limiter (ContextVar-based, per-turn)
-----------------------------------------
``_PROGRESS_STATE`` holds a dict keyed by turn_id:
  { "count": int, "last_ts": float }

Rules:
  - Single-step turns (compound-turn count == 1) → skip all emits.
  - Text emissions: max 4 per turn. Min 1.5 s gap between sends.
    Violations are silently dropped (log at DEBUG).
  - Typing actions: not capped, not gapped (cheap; Telegram ignores duplicates).

chat_id
-------
Read from ``agents.runtime.owner_id()`` as the fallback; the live bridge
supplies the real chat_id via runtime state where available.
"""
from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from typing import Any

from claude_agent_sdk import tool

from tools._annotations import annotations_for
from tools._response import ok as _ok

logger = logging.getLogger(__name__)

# --- rate-limit constants ---
_MAX_TEXT_EMISSIONS = 4
_MIN_GAP_SEC = 1.5
_SHORT_MSG_THRESHOLD = 60    # chars — below this, auto-mode picks typing

# ContextVar holding per-turn rate-limit state.
# Value is a dict: {"count": int, "last_ts": float}
# Keyed so that a fresh dict is installed at the start of each turn by
# whoever sets the context (the runtime or the first progress call in a turn).
_PROGRESS_STATE: ContextVar[dict[str, Any]] = ContextVar(
    "hikari_progress_state", default={}
)


def _turn_state(turn_id: str | None) -> dict[str, Any]:
    """Return the per-turn progress state, creating it on first access."""
    state = _PROGRESS_STATE.get()
    # If there's no state yet, or it belongs to a different turn, reset.
    if not state or state.get("turn_id") != turn_id:
        state = {"turn_id": turn_id, "count": 0, "last_ts": 0.0}
        _PROGRESS_STATE.set(state)
    return state


def _is_single_step_turn() -> bool:
    """True when the current context is a single-tool/single-step turn.

    We detect this by checking whether the compound-turn machinery is active.
    In a plain user-turn (one prompt → one agent call), there is no compound
    context; the tool list for this turn comes from _LAST_TURN_TOOL_NAMES
    which is populated *after* the turn completes, so we cannot rely on it.

    Pragmatic heuristic: if no turn_id is set we are outside a user turn
    (e.g. a proactive run) → treat as multi-step (don't skip).
    If a turn_id exists but no explicit step_count is stored, we default to
    assuming multi-step so progress is visible — that errs on the side of
    being informative.
    """
    try:
        from agents.runtime import current_turn_id
        tid = current_turn_id()
    except ImportError:
        tid = None
    if tid is None:
        return False   # not in a user turn at all — don't block
    # Check for explicit single-step marker set by compound_turn dispatcher.
    state = _PROGRESS_STATE.get()
    return bool(state.get("single_step"))


def _get_chat_id() -> int | None:
    """Resolve chat_id from environment (owner_id fallback)."""
    try:
        from agents.runtime import owner_id
        return owner_id()
    except Exception:
        logger.debug("progress: could not resolve chat_id")
        return None


async def _send_typing(chat_id: int) -> None:
    """Send a Telegram typing indicator. Non-fatal on error."""
    try:
        # Lazy-import bot to avoid circular imports at module load.
        from telegram.constants import ChatAction as _ChatAction

        from agents.telegram_bridge import _get_current_bot  # type: ignore[attr-defined]
        bot = _get_current_bot()
        if bot is None:
            logger.debug("progress: no active bot, skipping typing action")
            return
        await bot.send_chat_action(chat_id=chat_id, action=_ChatAction.TYPING)
        logger.debug("progress: typing action sent to chat_id=%s", chat_id)
    except ImportError:
        logger.debug("progress: telegram_bridge._get_current_bot not available")
    except Exception:
        logger.warning("progress: send_chat_action failed", exc_info=True)


async def _send_text(chat_id: int, message: str) -> None:
    """Send a Telegram text message. Non-fatal on error."""
    try:
        from agents.telegram_bridge import _get_current_bot  # type: ignore[attr-defined]
        bot = _get_current_bot()
        if bot is None:
            logger.debug("progress: no active bot, skipping text send")
            return
        await bot.send_message(chat_id=chat_id, text=message)
        logger.debug("progress: text progress sent to chat_id=%s: %r", chat_id, message[:60])
    except ImportError:
        logger.debug("progress: telegram_bridge._get_current_bot not available")
    except Exception:
        logger.warning("progress: send_message failed", exc_info=True)


@tool(
    "progress",
    "Signal in-turn progress to the user. "
    "Use for multi-step chains so the user sees activity. "
    "Args: message (str) — what's happening; mode ('typing'|'text'|'auto', default 'auto'). "
    "Auto: short messages (<60 chars, no surprise) → typing indicator; longer → text. "
    "Rate-limited: max 4 text emissions per turn, 1.5 s gap. Single-step turns are skipped. "
    "typing actions are never capped. Non-fatal: always returns ok.",
    {"message": str, "mode": str, "surprise": bool},
    annotations=annotations_for("progress"),
)
async def progress(args: dict[str, Any]) -> dict[str, Any]:
    message = str(args.get("message") or "").strip()
    mode_raw = str(args.get("mode") or "auto").strip().lower()
    surprise = bool(args.get("surprise") or False)

    if not message:
        return _ok("skipped: empty message")

    # --- single-step guard ---
    if _is_single_step_turn():
        logger.debug("progress: single-step turn — skipping emit")
        return _ok("skipped: single-step turn")

    # --- resolve mode ---
    if mode_raw == "auto":
        short = len(message) < _SHORT_MSG_THRESHOLD
        mode = "typing" if (short and not surprise) else "text"
    elif mode_raw in ("typing", "text"):
        mode = mode_raw
    else:
        mode = "typing"   # unknown value → safe default

    # --- typing actions bypass the rate limiter ---
    if mode == "typing":
        chat_id = _get_chat_id()
        if chat_id is not None:
            await _send_typing(chat_id)
        return _ok(f"typing action sent (mode=typing message={message[:40]!r})")

    # --- text-mode rate limiter ---
    try:
        from agents.runtime import current_turn_id
        tid = current_turn_id()
    except ImportError:
        tid = None

    state = _turn_state(tid)
    now = time.monotonic()
    count = state.get("count", 0)
    last_ts = state.get("last_ts", 0.0)

    if count >= _MAX_TEXT_EMISSIONS:
        logger.debug(
            "progress: cap reached (%d/%d), dropping text emit: %r",
            count, _MAX_TEXT_EMISSIONS, message[:40],
        )
        return _ok(f"skipped: text cap reached ({count}/{_MAX_TEXT_EMISSIONS})")

    gap = now - last_ts
    if last_ts > 0 and gap < _MIN_GAP_SEC:
        logger.debug(
            "progress: gap too small (%.2fs < %.1fs), dropping text emit: %r",
            gap, _MIN_GAP_SEC, message[:40],
        )
        return _ok(f"skipped: gap too small ({gap:.2f}s < {_MIN_GAP_SEC}s)")

    # Update state before sending (optimistic — prevents burst even if send fails).
    state["count"] = count + 1
    state["last_ts"] = now

    chat_id = _get_chat_id()
    if chat_id is not None:
        await _send_text(chat_id, message)

    return _ok(
        f"text progress sent ({state['count']}/{_MAX_TEXT_EMISSIONS}): {message[:60]!r}"
    )
