"""Background listener — single asyncio task that drains tools.dispatch.DISPATCH_EVENTS
and converts events into in-voice Telegram messages.

Per-task state:
  - last_progress_sent_at: throttle progress pings to 1/min per task
  - drain task running

The listener runs forever. Started in agents/telegram_bridge.post_init.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING, Any

from storage import db
from tools.dispatch import DISPATCH_EVENTS

from . import config as cfg
from . import post_filter
from .post_filter import filter_outgoing

if TYPE_CHECKING:
    from telegram import Bot

logger = logging.getLogger(__name__)


def _progress_debounce_sec() -> float:
    return float(cfg.get("dispatch.progress_debounce_sec", 60.0))


def _hard_task_duration_s() -> float:
    return float(cfg.get("dispatch.hard_task_duration_s", 600.0))


def _hard_task_summary_chars() -> int:
    return int(cfg.get("dispatch.hard_task_summary_chars", 2000))


def _hard_task_tool_uses() -> int:
    return int(cfg.get("dispatch.hard_task_tool_uses", 3))


def _progress_line(tool_uses: int) -> str:
    pool = cfg.get("dispatch.progress_lines") or ["still going."]
    base = random.choice(pool)
    if tool_uses > 0:
        return f"{base} ({tool_uses} tool uses so far)"
    return base


def _done_line(summary: str, cost: float, duration_s: float) -> str:
    mins = max(1, int(duration_s // 60))
    head = f"done. {mins}m, ~${cost:.2f}."
    snippet = summary.strip()
    if not snippet:
        return head
    # Take first ~400 chars of the summary so Hikari can react in voice if she wants.
    if len(snippet) > 400:
        snippet = snippet[:400].rstrip() + "…"
    return f"{head}\n\n{snippet}"


def _failed_line(reason: str, duration_s: float) -> str:
    mins = max(1, int(duration_s // 60))
    return f"that fell over after ~{mins}m. {reason}. want me to retry?"


def _is_hard_task(meta: dict[str, Any]) -> bool:
    duration = float(meta.get("duration_s") or 0)
    summary_len = len(meta.get("summary") or "")
    tool_uses = int(meta.get("tool_uses") or 0)
    return (
        duration >= _hard_task_duration_s()
        or summary_len >= _hard_task_summary_chars()
        or tool_uses >= _hard_task_tool_uses()
    )


async def listener_loop(bot: Bot) -> None:
    """Forever-running drain. One pass per queued event."""
    logger.info("background dispatch listener started")
    # Per-task throttle: task_id -> last progress emit time
    last_progress: dict[str, float] = {}
    while True:
        try:
            task_id, event_type, payload = await DISPATCH_EVENTS.get()
        except asyncio.CancelledError:
            logger.info("dispatch listener cancelled")
            return
        try:
            await _handle_event(bot, task_id, event_type, payload, last_progress)
        except Exception:  # noqa: BLE001
            logger.exception("listener: failed to handle %s for %s", event_type, task_id)


async def _handle_event(bot: Bot, task_id: str, event_type: str,
                        payload: dict[str, Any], last_progress: dict[str, float]) -> None:
    row = db.bg_task_get(task_id)
    if not row:
        logger.warning("listener: unknown task_id %s", task_id)
        return
    chat_id = int(row["chat_id"])

    if event_type == "started":
        # Hikari already acked the dispatch in voice via the tool return.
        # No additional message needed here.
        return

    if event_type == "tool_use":
        now = time.monotonic()
        last = last_progress.get(task_id, 0.0)
        if now - last < _progress_debounce_sec():
            return
        last_progress[task_id] = now
        line = _progress_line(int(payload.get("count") or 0))
        await _safe_send(bot, chat_id, line)
        return

    if event_type == "done":
        last_progress.pop(task_id, None)
        line = _done_line(
            summary=str(payload.get("summary") or ""),
            cost=float(payload.get("cost") or 0.0),
            duration_s=float(payload.get("duration_s") or 0.0),
        )
        await _safe_send(bot, chat_id, line)
        if _is_hard_task(payload):
            # Triggered as a fire-and-forget; Phase 5 reflection.
            from agents.reflection import reflection_after_task
            asyncio.create_task(reflection_after_task(task_id))
        return

    if event_type == "failed":
        last_progress.pop(task_id, None)
        line = _failed_line(
            reason=str(payload.get("reason") or "unknown"),
            duration_s=float(payload.get("duration_s") or 0.0),
        )
        await _safe_send(bot, chat_id, line)
        return

    logger.warning("listener: unhandled event_type %r for %s", event_type, task_id)


async def _safe_send(bot: Bot, chat_id: int, text: str) -> None:
    """Run the outgoing filter then send. Same filter applied to interactive
    replies, so listener messages can't leak safety-voice patter either.

    Phase 8: when the filter flags a rewrite-worthy hit, attempt one bounded
    Haiku rewrite before falling back to a deterministic short reply.
    """
    try:
        filtered = filter_outgoing(text)
        to_send = filtered.text
        if filtered.refusal_short_replaced:
            db.append_thought(
                "listener: short-replaced safety-voice in dispatch ping. "
                f"hits={filtered.refusal_hits[:3]}"
            )
        elif filtered.needs_llm_rewrite:
            to_send = await post_filter.rewrite_or_fallback(
                text, filtered, mood=None, where="listener",
            )
        await bot.send_message(chat_id=chat_id, text=to_send)
    except Exception:  # noqa: BLE001
        logger.exception("listener: send_message failed for chat %s", chat_id)


async def recover_running_tasks(bot: Bot) -> None:
    """On bridge startup, send an apology for any task that was running mid-restart."""
    rows = db.bg_tasks_running()
    if not rows:
        return
    for row in rows:
        chat_id = int(row["chat_id"])
        task_id = row["task_id"]
        # We can't actually resume a Python-side asyncio.Task — the SDK session id is
        # persisted but the in-process worker is gone. Mark failed, apologize.
        db.bg_task_update(
            task_id, status="failed",
            completed_at=db._now(),
            result_summary="bot restarted mid-task; not resumed",
        )
        await _safe_send(
            bot, chat_id,
            f"that task (id {task_id[:8]}) got cut off by a restart. "
            "say the word and i'll re-dispatch.",
        )


async def recover_deferred_approvals(bot: Bot) -> None:
    """Phase 6: on startup, resurface any approval that was pending when the
    bot died. The deferred-row carries enough state (tool_use_id, args) for
    the resume path to work once the user replies.

    We use the same prompt format as the original defer prompt + a config-
    driven suffix so the user knows it's a resurrection, not a duplicate.
    """
    from agents import config as cfg
    from tools import approvals as approval_tools

    pending = db.approvals_pending_deferred()
    if not pending:
        return
    suffix = cfg.get(
        "approvals.defer_restart_resurface_suffix",
        " (still waiting on this from before the restart.)",
    )
    for row in pending:
        chat_id = int(row["chat_id"])
        tier = int(row["tier"])
        summary = str(row["summary"]) + suffix
        try:
            await approval_tools.send_defer_prompt(
                chat_id=chat_id, tier=tier, summary=summary,
            )
        except Exception:
            logger.exception(
                "recover_deferred_approvals: send failed for approval %s",
                row.get("id"),
            )
        else:
            logger.info(
                "recover_deferred_approvals: resurfaced approval %s (tool=%s)",
                row.get("id"), row.get("tool_name"),
            )
