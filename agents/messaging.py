"""Centralised outgoing-message API.

Single send-and-persist path for all sources. Handles filter, choreography,
Telegram send, and DB persistence in one place so callers never duplicate
the sequence.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import pathlib
import time
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

Source = Literal[
    "chat",
    "proactive",
    "reaction",
    "daily_checkin",
    "morning_brief",
    "decision_log",
    "event",
]

_MAX_TYPING_SLEEP = 2.5  # seconds


@dataclass(frozen=True)
class SendResult:
    final_text: str
    telegram_message_id: int | None
    ok: bool

    def __iter__(self):
        yield self.final_text
        yield self.telegram_message_id
        yield self.ok


async def send_and_persist(
    *,
    bot,
    chat_id: int,
    text: str,
    source: Source,
    reply_to=None,
    photo_path: pathlib.Path | None = None,
    elapsed_real: float = 0.0,
    skip_choreography: bool = False,
    persist: bool = True,
    run_hooks: bool = True,
    db=None,
) -> SendResult:
    """Send a message through Telegram and persist it to the DB.

    Parameters
    ----------
    bot:
        python-telegram-bot Bot instance (or compatible fake).
    chat_id:
        Telegram chat to send to.
    text:
        Draft text from the LLM. May be empty when photo_path is set.
    source:
        Discriminator written to the ``messages`` row.
    reply_to:
        telegram.Message to reply to (calls reply_text). Mutually exclusive
        with the default bot.send_message path.
    photo_path:
        If set, sends a photo with text as caption. Filter is skipped for
        photo-only sends (empty text).
    elapsed_real:
        Seconds the agent spent generating — used to scale typing delay so
        choreography reflects actual latency.
    skip_choreography:
        Skip typing indicator entirely (e.g. for proactive sends that already
        waited elsewhere).
    persist:
        Write a ``messages`` row on success. Pass False for dry-run paths.
    run_hooks:
        Reserved for future post-send hook dispatch by the bridge.
    db:
        storage.db module override for testing. Defaults to a lazy import.
    """
    if not text and photo_path is None:
        return SendResult("", None, False)

    _db = db
    if _db is None:
        from storage import db as _db_mod
        _db = _db_mod

    final_text = text

    # Apply post-filter only when there is text to filter.
    if text:
        from agents.post_filter import filter_outgoing, rewrite_or_fallback

        result = filter_outgoing(text)
        if result.needs_llm_rewrite:
            final_text = await rewrite_or_fallback(text, result, mood=None)
        else:
            final_text = result.text

    # Compute idempotency key from stable inputs BEFORE the Telegram call.
    created_at_ms = int(time.time() * 1000)
    kind_str = "photo" if photo_path is not None else "text"
    _ikey_raw = f"{kind_str}{final_text or ''}{created_at_ms}"
    idempotency_key = hashlib.sha256(_ikey_raw.encode()).hexdigest()[:32]

    # Insert media_outbox row BEFORE the Telegram call (crash-safe durability).
    _outbox_row_id: int | None = None
    try:
        _outbox_row_id = _db.media_outbox_insert(
            kind_str,
            idempotency_key,
            {
                "chat_id": chat_id,
                "source": source,
                "text": final_text,
                "photo_path": str(photo_path) if photo_path is not None else None,
            },
        )
    except Exception:
        logger.exception("send_and_persist: media_outbox pre-insert failed (non-fatal)")

    # Typing choreography: best-effort, non-fatal.
    if not skip_choreography and text and hasattr(bot, "send_chat_action"):
        try:
            await bot.send_chat_action(chat_id, "typing")
            delay = min(len(final_text) / 40, _MAX_TYPING_SLEEP)
            await asyncio.sleep(delay)
        except Exception:  # noqa: BLE001
            logger.debug("send_and_persist: typing action failed (non-fatal)")

    # Send the message.
    tg_msg = None
    try:
        if photo_path is not None:
            with open(photo_path, "rb") as photo_fh:
                tg_msg = await bot.send_photo(
                    chat_id=chat_id,
                    photo=photo_fh,
                    caption=final_text or None,
                )
        elif reply_to is not None:
            tg_msg = await reply_to.reply_text(final_text)
        else:
            tg_msg = await bot.send_message(chat_id=chat_id, text=final_text)
    except Exception as _exc:
        logger.exception("send_and_persist: Telegram send failed")
        if _outbox_row_id is not None:
            try:
                _db.media_outbox_mark_failed(_outbox_row_id, str(_exc))
            except Exception:
                logger.exception("send_and_persist: media_outbox_mark_failed failed (non-fatal)")
        return SendResult(final_text, None, False)

    tg_msg_id: int | None = getattr(tg_msg, "message_id", None)

    # Mark outbox row sent.
    if _outbox_row_id is not None:
        try:
            _db.media_outbox_mark_sent(_outbox_row_id, tg_msg_id)
        except Exception:
            logger.exception("send_and_persist: media_outbox_mark_sent failed (non-fatal)")

    # Persist to DB after a confirmed send.
    if persist and final_text:
        try:
            if tg_msg_id is not None:
                _db.append_message_with_telegram_id(
                    "assistant", final_text, tg_msg_id, source=source
                )
            else:
                _db.append_message("assistant", final_text, source=source)
        except Exception:
            logger.exception("send_and_persist: DB persist failed (send was ok)")

    # run_hooks is reserved — callers (bridge) invoke hooks themselves.
    _ = run_hooks

    return SendResult(final_text, tg_msg_id, True)
