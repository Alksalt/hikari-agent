"""Phase 9 Stage C — non-verbal reply modes.

Two probability-gated paths short-circuit the normal text reply on inbound
user messages:

  - **sticker-only**: ship a sticker from ``stickers.pool`` and write a
    ``[sticker-only]`` marker into ``messages`` so memory / drift telemetry
    still see that *something* happened.
  - **reaction-only**: ship a Telegram message reaction (via
    ``setMessageReaction``) and write a ``[reaction-only: <emoji>]`` marker.

The flow:
  1. ``maybe_nonverbal_reply`` decides if a non-verbal mode fires for this
     turn. Heuristics (skipped on questions, long messages, irritable mood
     for sticker-only, etc.) and a daily-cap shared with reaction-turns
     guard against runaway use.
  2. Bridge inspects the return value. ``None`` → continue to ``respond()``
     and ship a text reply as usual. Non-``None`` → bridge sends the
     non-verbal artifact and returns; ``respond()`` is never invoked.

Money discipline: the whole point of non-verbal modes is that they're
*free* — no LLM call. Probabilities are small (default 3% sticker, 5%
reaction) so they're a sprinkle, not a habit.
"""

from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING, Literal

from storage import db

from . import config as cfg
from . import reactions as reactions_mod
from . import stickers as stickers_mod

if TYPE_CHECKING:
    from telegram import Bot

logger = logging.getLogger(__name__)

NonverbalKind = Literal["sticker", "reaction"]


_DAILY_KEY = "nonverbal_count_day"
_DAY_KEY = "nonverbal_count_date"


def _sticker_solo_prob() -> float:
    return float(cfg.get("stickers.solo_reply_probability", 0.03))


def _reaction_solo_prob() -> float:
    return float(cfg.get("reactions.solo_reaction_probability", 0.05))


def _max_per_day() -> int:
    return int(cfg.get("nonverbal.max_per_day", 25))


def _min_text_for_substantive() -> int:
    """User messages this long or longer get a real text reply, never a
    non-verbal placeholder."""
    return int(cfg.get("nonverbal.min_text_for_substantive", 60))


def _mood_blocklist_sticker() -> list[str]:
    return [str(m).strip().lower() for m in (cfg.get("stickers.mood_blocklist") or [])]


def _today_iso() -> str:
    from datetime import UTC, datetime
    return datetime.now(UTC).date().isoformat()


def _peek_count() -> int:
    if db.runtime_get(_DAY_KEY) != _today_iso():
        return 0
    return db.runtime_get_int(_DAILY_KEY, 0)


def _bump_count() -> int:
    """Phase 9 review-F3: use atomic ``db.runtime_increment`` so concurrent
    bumps don't lose a count."""
    today = _today_iso()
    if db.runtime_get(_DAY_KEY) != today:
        db.runtime_set(_DAY_KEY, today)
        db.runtime_set(_DAILY_KEY, "0")
    return db.runtime_increment(_DAILY_KEY, 1)


_SUBSTANTIVE_OPENERS = (
    "can ", "could ", "would ", "will ", "explain ", "tell me", "help ",
    "show ", "what about", "what's ", "what is", "how do", "how does",
    "how can", "how should", "why ", "remind me", "remember when",
    "did i ", "do i ", "did you", "do you",
)


def _looks_substantive(user_text: str) -> bool:
    """Heuristic — never short-circuit a substantive user message with a
    non-verbal artifact. Questions, long messages, and common conversational
    openers ("can you", "explain", "what about") all force a real reply.

    Phase 9 review-F4: prefix-match openers added so short imperative or
    interrogative messages without a literal ``?`` still get a substantive
    response (e.g. "can you check that thing" is 25 chars + no question
    mark, yet clearly deserves a reply, not a sticker).
    """
    t = (user_text or "").strip()
    if not t:
        return False
    if "?" in t:
        return True
    if len(t) >= _min_text_for_substantive():
        return True
    t_lower = t.lower()
    if any(t_lower.startswith(opener) for opener in _SUBSTANTIVE_OPENERS):
        return True
    return False


def maybe_nonverbal_reply(user_text: str, mood: str) -> NonverbalKind | None:
    """Decide whether to ship a non-verbal reply this turn.

    Returns the kind to ship, or ``None`` to fall through to the normal
    ``respond()`` path. Caller is responsible for actually sending the
    artifact via :func:`send_sticker_only` / :func:`send_reaction_only`.
    """
    if not user_text or not user_text.strip():
        return None
    if _looks_substantive(user_text):
        return None
    if _peek_count() >= _max_per_day():
        return None
    mood_lower = (mood or "").strip().lower()

    # Sticker-only roll first (rarer / heavier than a reaction).
    sticker_pool = list(cfg.get("stickers.pool") or [])
    sticker_enabled = bool(cfg.get("stickers.enabled", True))
    if (
        sticker_enabled
        and sticker_pool
        and mood_lower not in _mood_blocklist_sticker()
        and random.random() < _sticker_solo_prob()
    ):
        return "sticker"

    # Reaction-only roll.
    reaction_pool = list(cfg.get("reactions.pool") or [])
    reaction_enabled = bool(cfg.get("reactions.enabled", True))
    if (
        reaction_enabled
        and reaction_pool
        and random.random() < _reaction_solo_prob()
    ):
        return "reaction"

    return None


async def send_sticker_only(bot: "Bot", chat_id: int) -> str | None:
    """Ship a sticker, write a [sticker-only] marker to messages.
    Returns the file_id sent (or None if pool empty / send failed)."""
    file_id = stickers_mod.pick_sticker_file_id()
    if file_id is None:
        return None
    try:
        await bot.send_sticker(chat_id=chat_id, sticker=file_id)
    except Exception:
        logger.exception("nonverbal: send_sticker failed (non-fatal)")
        return None
    _bump_count()
    try:
        db.append_message("assistant", "[sticker-only]")
    except Exception:
        logger.exception("nonverbal: marker write failed (non-fatal)")
    return file_id


async def send_reaction_only(
    bot: "Bot", chat_id: int, message_id: int,
) -> str | None:
    """Ship a reaction on the user's message, write a [reaction-only: emoji]
    marker to messages. Returns the emoji used (or None on failure)."""
    emoji = reactions_mod.pick_emoji()
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
        logger.exception("nonverbal: set_message_reaction failed (non-fatal)")
        return None
    _bump_count()
    try:
        db.append_message("assistant", f"[reaction-only: {emoji}]")
    except Exception:
        logger.exception("nonverbal: marker write failed (non-fatal)")
    return emoji
