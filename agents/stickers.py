"""Outbound sticker sender — a cheap character-density win, mirror of reactions.

A sticker is sent AFTER Hikari's text reply has shipped, via the `sendSticker`
Bot API. We use it sparingly:

  - probability_per_reply (default 0.05)
  - cooldown_min_messages: don't send if we sent one within the last N outbound
    replies (prevents tic-like spam)
  - mood_blocklist: skip entirely when the day's mood matches (default: irritable)

Picks from `stickers.pool` in config. The pool ships EMPTY by default — fill
with Telegram file_ids once curated. Empty pool IS the disable state.

State is kept in runtime_state:
  - outbound_message_counter (int) — count of outbound replies since process start
    (shared via storage.db.OUTBOUND_MSG_COUNTER_KEY)
  - stickers_last_at_counter (int) — outbound_counter value at time of last send
"""

from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING

from storage import db
from storage.db import OUTBOUND_MSG_COUNTER_KEY

from . import config as cfg

if TYPE_CHECKING:
    from telegram import Bot

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return bool(cfg.get("stickers.enabled", True))


def _probability() -> float:
    return float(cfg.get("stickers.probability_per_reply", 0.05))


def _cooldown() -> int:
    return int(cfg.get("stickers.cooldown_min_messages", 12))


def _pool() -> list[str]:
    return list(cfg.get("stickers.pool") or [])


def _mood_blocklist() -> list[str]:
    return [str(m).strip().lower() for m in (cfg.get("stickers.mood_blocklist") or [])]


def _current_mood() -> str:
    return (db.get_core_block("mood_today") or "focused").strip().lower() or "focused"


def _bump_outbound_counter() -> int:
    """Increment the shared outbound-message counter. Returns new value.

    The counter is shared via storage.db.OUTBOUND_MSG_COUNTER_KEY so other
    modules can read the same outbound tally without re-counting.
    """
    n = db.runtime_get_int(OUTBOUND_MSG_COUNTER_KEY, 0) + 1
    db.runtime_set(OUTBOUND_MSG_COUNTER_KEY, n)
    return n


def _record_sticker(at_counter: int) -> None:
    db.runtime_set("stickers_last_at_counter", at_counter)


def should_send_sticker(now_counter: int) -> bool:
    if not _enabled():
        return False
    pool = _pool()
    if not pool:
        return False
    if _current_mood() in _mood_blocklist():
        return False
    last_at = db.runtime_get_int("stickers_last_at_counter", 0)
    # last_at == 0 means we've never sent one — cooldown doesn't apply yet.
    if last_at > 0 and (now_counter - last_at) < _cooldown():
        return False
    return random.random() < _probability()


def pick_sticker_file_id() -> str | None:
    pool = _pool()
    if not pool:
        return None
    return random.choice(pool)


async def maybe_send_sticker(bot: Bot, chat_id: int, outbound_counter: int) -> str | None:
    """Roll the sticker gate; on success, send the sticker. Returns the file_id
    sent (or None if vetoed). Never raises — Telegram API errors are logged.

    Call this AFTER the text reply has shipped. ``outbound_counter`` should be
    the current value of the shared outbound counter (caller is responsible for
    bumping it via _bump_outbound_counter or by passing a freshly-bumped value)."""
    if not should_send_sticker(outbound_counter):
        return None
    file_id = pick_sticker_file_id()
    if file_id is None:
        return None
    try:
        await bot.send_sticker(chat_id=chat_id, sticker=file_id)
    except Exception:
        logger.exception("stickers: send_sticker failed (non-fatal)")
        return None
    _record_sticker(outbound_counter)
    return file_id
