"""Telegram message reactions — a cheap character-density win.

A reaction is a single emoji attached to an inbound user message via the
`setMessageReaction` Bot API. We use it sparingly:

  - probability_per_inbound (default 0.10)
  - cooldown_min_messages: don't react if we reacted within the last N inbound
    user turns (prevents tic-like over-reaction)

Picks from `reactions.pool` in config. The pool should match Hikari's
non-emotive flirt grammar — `[reads it twice]` energy, never enthusiastic.

State is kept in runtime_state:
  - reactions_inbound_counter (int) — count of inbound messages since process start
  - reactions_last_at_counter (int) — inbound_counter value at time of last reaction
"""

from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING

from storage import db
from storage.db import INBOUND_MSG_COUNTER_KEY

from . import config as cfg

if TYPE_CHECKING:
    from telegram import Bot

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return bool(cfg.get("reactions.enabled", True))


def _probability() -> float:
    return float(cfg.get("reactions.probability_per_inbound", 0.10))


def _cooldown() -> int:
    return int(cfg.get("reactions.cooldown_min_messages", 8))


def _pool() -> list[str]:
    return list(cfg.get("reactions.pool") or [])


def _bump_inbound_counter() -> int:
    """Increment the shared inbound-message counter. Returns new value.

    The counter is shared across modules (location uses it for defer gating)
    via storage.db.INBOUND_MSG_COUNTER_KEY.
    """
    n = db.runtime_get_int(INBOUND_MSG_COUNTER_KEY, 0) + 1
    db.runtime_set(INBOUND_MSG_COUNTER_KEY, n)
    return n


def _record_reaction(at_counter: int) -> None:
    db.runtime_set("reactions_last_at_counter", at_counter)


def should_react(now_counter: int) -> bool:
    if not _enabled():
        return False
    pool = _pool()
    if not pool:
        return False
    last_at = db.runtime_get_int("reactions_last_at_counter", 0)
    # last_at == 0 means we've never reacted — cooldown doesn't apply yet.
    if last_at > 0 and (now_counter - last_at) < _cooldown():
        return False
    return random.random() < _probability()


def pick_emoji() -> str | None:
    pool = _pool()
    if not pool:
        return None
    return random.choice(pool)


async def maybe_react(bot: Bot, chat_id: int, message_id: int) -> str | None:
    """Roll the reaction gate; on success, send the reaction. Returns the emoji
    sent (or None if vetoed). Never raises — Telegram API errors are logged."""
    n = _bump_inbound_counter()
    if not should_react(n):
        return None
    emoji = pick_emoji()
    if emoji is None:
        return None
    try:
        from telegram import ReactionTypeEmoji
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
    except Exception:
        logger.exception("reactions: set_message_reaction failed (non-fatal)")
        return None
    _record_reaction(n)
    return emoji
