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

from agents.runtime import _call_aux_llm
from storage import db
from storage.db import OUTBOUND_MSG_COUNTER_KEY

from . import config as cfg
from .cadence import _warmth_band

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
  - rare playful escalation
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


def _warmth_factor() -> float:
    """Scale factor derived from the current warmth band (same source as cadence.py).

    low  → cycle_modulation.low_tolerance_proactive_cap_scale  (default 0.5)
    open → cycle_modulation.open_proactive_cap_scale            (default 1.25)
    mid / None (disabled / absent) → 1.0
    """
    band = _warmth_band()
    if band == "low":
        return float(cfg.get("cycle_modulation.low_tolerance_proactive_cap_scale", 0.5))
    if band == "open":
        return float(cfg.get("cycle_modulation.open_proactive_cap_scale", 1.25))
    return 1.0


def _effective_probability() -> float:
    """Base probability scaled by the current warmth band, clamped to [0, 1]."""
    return min(1.0, max(0.0, _probability() * _warmth_factor()))


def _cooldown() -> int:
    return int(cfg.get("stickers.cooldown_min_messages", 12))


_WARNED_BAD_POOL_ENTRIES: set[str] = set()


def _pool() -> list[dict]:
    """Return pool entries as list of {file_id, description} dicts.

    Accepts both legacy flat strings and new dict format for backwards compat.
    Malformed entries (missing file_id, wrong type) are dropped with a one-shot
    warning per session — silent drops previously made a botched yaml edit
    invisible (/status would still report the raw count).
    """
    raw = cfg.get("stickers.pool") or []
    entries: list[dict] = []
    for idx, item in enumerate(raw):
        if isinstance(item, dict) and item.get("file_id"):
            entries.append({"file_id": str(item["file_id"]), "description": str(item.get("description", ""))})
        elif isinstance(item, str) and item:
            entries.append({"file_id": item, "description": ""})
        else:
            key = f"{idx}:{type(item).__name__}"
            if key not in _WARNED_BAD_POOL_ENTRIES:
                _WARNED_BAD_POOL_ENTRIES.add(key)
                logger.warning("stickers: pool entry %d malformed: %r", idx, item)
    return entries


def pool_counts() -> dict[str, int]:
    """Return {"raw": int, "valid": int} for /status visibility into pool health."""
    raw = cfg.get("stickers.pool") or []
    return {"raw": len(raw), "valid": len(_pool())}


def _mood_blocklist() -> list[str]:
    return [str(m).strip().lower() for m in (cfg.get("stickers.mood_blocklist") or [])]


def _current_mood() -> str:
    return (db.get_core_block("mood_today") or "focused").strip().lower() or "focused"


def _bump_outbound_counter() -> int:
    """Increment the shared outbound-message counter atomically. Returns new value."""
    return db.runtime_increment(OUTBOUND_MSG_COUNTER_KEY, by=1)


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
    return random.random() < _effective_probability()


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
        result = (await _call_aux_llm(prompt, system=system)).strip().strip('"')
    except Exception as exc:
        # Promoted to warning so an oncall can see "situational selection is
        # silently degraded to random because aux LLM is down". Default INFO
        # level swallowed this signal previously.
        logger.warning(
            "pick_sticker_file_id: aux LLM failed (%s) — falling back to random",
            type(exc).__name__,
        )
        return random.choice(pool)["file_id"]

    if result == "none":
        return None

    valid_ids = {e["file_id"] for e in pool}
    if result in valid_ids:
        return result

    logger.warning(
        "pick_sticker_file_id: LLM returned unknown id %r — "
        "annotating diary and falling back to random",
        result,
    )
    try:
        with db._conn() as conn:
            conn.execute(
                "INSERT INTO character_thoughts (thought, created_at) VALUES (?, datetime('now'))",
                (
                    f"[sticker] aux LLM hallucinated a sticker id that isn't in my pool: "
                    f"{result!r}. falling back to random pick.",
                ),
            )
    except Exception:
        logger.debug("pick_sticker_file_id: could not write hallucination note to diary")
    return random.choice(pool)["file_id"]


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
            claim_inline=True,  # sent in-line below; drain must not re-send it
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
