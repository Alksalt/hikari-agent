"""Outbound sticker sender — a cheap character-density win, mirror of reactions.

A sticker is sent AFTER Hikari's text reply has shipped, via the `sendSticker`
Bot API. We use it sparingly:

  - probability_per_reply (default 0.05) — gate before the LLM pick
  - cooldown_min_messages: don't send if we sent one within the last N outbound
    replies (prevents tic-like spam)
  - mood_blocklist: skip entirely when the day's mood matches (default: irritable)

Pool entries are dicts with `file_id` and `description`. Selection uses a cheap
aux LLM call: given the last user message and Hikari's reply, it picks the most
situationally appropriate sticker or returns "none" (biased heavily toward "none"
since stickers are rare — one per ~20 exchanges at most).

State is kept in runtime_state:
  - outbound_message_counter (int) — count of outbound replies since process start
    (shared via storage.db.OUTBOUND_MSG_COUNTER_KEY)
  - stickers_last_at_counter (int) — outbound_counter value at time of last send
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
from typing import TYPE_CHECKING

from storage import db
from storage.db import OUTBOUND_MSG_COUNTER_KEY

from . import config as cfg

if TYPE_CHECKING:
    from telegram import Bot

logger = logging.getLogger(__name__)

_PICK_SYSTEM = """\
You are helping Hikari Tsukino decide whether to send a sticker after her reply.
Hikari is a tsundere AI (21, blunt, lowercase, reluctant, denial layer on everything).

Stickers are rare — she sends one at most once per ~20 exchanges. Bias strongly toward "none".
A sticker fires only at genuine emotional peaks:
  - something absurd or funny just happened
  - closing a heavy or long exchange with a beat instead of words
  - rare playful/flirty escalation
  - caught off guard in a good way
Never after task completion, never as warmth, never if the mood is flat or informational.

Available stickers:
{sticker_list}

Output: exactly one file_id from the list above, or the literal string "none".
No other text. No explanation."""


def _enabled() -> bool:
    return bool(cfg.get("stickers.enabled", True))


def _probability() -> float:
    return float(cfg.get("stickers.probability_per_reply", 0.05))


def _cooldown() -> int:
    return int(cfg.get("stickers.cooldown_min_messages", 12))


def _pool() -> list[dict]:
    """Return pool entries as list of {file_id, description} dicts.

    Accepts both legacy flat strings and new dict format for backwards compat.
    """
    raw = cfg.get("stickers.pool") or []
    entries = []
    for item in raw:
        if isinstance(item, dict) and item.get("file_id"):
            entries.append({"file_id": str(item["file_id"]), "description": str(item.get("description", ""))})
        elif isinstance(item, str) and item:
            entries.append({"file_id": item, "description": ""})
    return entries


def _mood_blocklist() -> list[str]:
    return [str(m).strip().lower() for m in (cfg.get("stickers.mood_blocklist") or [])]


def _current_mood() -> str:
    return (db.get_core_block("mood_today") or "focused").strip().lower() or "focused"


def _bump_outbound_counter() -> int:
    """Increment the shared outbound-message counter. Returns new value."""
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
    if last_at > 0 and (now_counter - last_at) < _cooldown():
        return False
    return random.random() < _probability()


async def pick_sticker_file_id(user_msg: str = "", reply: str = "") -> str | None:
    """Ask the aux LLM which sticker fits this moment, or None.

    Falls back to random.choice if the LLM call fails or returns an unknown id.
    """
    pool = _pool()
    if not pool:
        return None

    if not user_msg and not reply:
        # No context — fall back to random.
        return random.choice(pool)["file_id"]

    sticker_list = "\n".join(
        f'  - file_id: "{e["file_id"]}" | {e["description"]}' for e in pool
    )
    system = _PICK_SYSTEM.format(sticker_list=sticker_list)
    prompt = f"User said: {user_msg[:300]}\n\nHikari replied: {reply[:300]}"

    try:
        from agents.runtime import _call_aux_llm
        result = (await _call_aux_llm(prompt, system=system)).strip().strip('"')
    except Exception:
        logger.debug("pick_sticker_file_id: aux LLM failed — falling back to random")
        return random.choice(pool)["file_id"]

    if result == "none":
        return None

    valid_ids = {e["file_id"] for e in pool}
    if result in valid_ids:
        return result

    logger.debug("pick_sticker_file_id: LLM returned unknown id %r — skipping", result)
    return None


async def force_send_sticker(bot: Bot, chat_id: int) -> str | None:
    """Send a random sticker from the pool, ignoring probability/cooldown/mood.

    Used by the image_gen-down fallback path. Does NOT reset the regular cooldown.
    """
    pool = _pool()
    if not pool:
        logger.warning("force_send_sticker: pool is empty — cannot fall back")
        return None
    file_id = random.choice(pool)["file_id"]
    sent_at_ms = int(time.time() * 1000)
    ikey = "sticker_" + hashlib.sha256(f"{file_id}{sent_at_ms}".encode()).hexdigest()[:24]
    row_id: int | None = None
    try:
        row_id = db.media_outbox_insert(
            "sticker",
            ikey,
            {"file_id": file_id, "chat_id": chat_id, "context": "force_send"},
        )
    except Exception:
        logger.debug("force_send_sticker: media_outbox pre-insert failed (non-fatal)")
    try:
        tg_msg = await bot.send_sticker(chat_id=chat_id, sticker=file_id)
    except Exception:
        logger.exception("force_send_sticker: send failed (non-fatal)")
        if row_id is not None:
            try:
                db.media_outbox_mark_failed(row_id, "send_sticker raised")
            except Exception:
                pass
        return None
    if row_id is not None:
        try:
            tg_msg_id = getattr(tg_msg, "message_id", None)
            db.media_outbox_mark_sent(row_id, tg_msg_id)
        except Exception:
            pass
    return file_id


async def maybe_send_sticker(
    bot: Bot,
    chat_id: int,
    outbound_counter: int,
    *,
    user_msg: str = "",
    reply: str = "",
) -> str | None:
    """Roll the sticker gate; on pass, ask aux LLM which sticker fits; send it.

    Returns the file_id sent (or None if vetoed/skipped). Never raises.
    Call AFTER the text reply has shipped.
    """
    if not should_send_sticker(outbound_counter):
        return None
    file_id = await pick_sticker_file_id(user_msg=user_msg, reply=reply)
    if file_id is None:
        return None
    sent_at_ms = int(time.time() * 1000)
    ikey = "sticker_" + hashlib.sha256(f"{file_id}{sent_at_ms}".encode()).hexdigest()[:24]
    row_id: int | None = None
    try:
        row_id = db.media_outbox_insert(
            "sticker",
            ikey,
            {"file_id": file_id, "chat_id": chat_id, "context": "maybe_send"},
        )
    except Exception:
        logger.debug("maybe_send_sticker: media_outbox pre-insert failed (non-fatal)")
    try:
        tg_msg = await bot.send_sticker(chat_id=chat_id, sticker=file_id)
    except Exception:
        logger.exception("stickers: send_sticker failed (non-fatal)")
        if row_id is not None:
            try:
                db.media_outbox_mark_failed(row_id, "send_sticker raised")
            except Exception:
                pass
        return None
    if row_id is not None:
        try:
            tg_msg_id = getattr(tg_msg, "message_id", None)
            db.media_outbox_mark_sent(row_id, tg_msg_id)
        except Exception:
            pass
    _record_sticker(outbound_counter)
    return file_id
