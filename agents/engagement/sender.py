"""Sender: wraps the send_text choreography function and writes one row to
proactive_events."""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

from agents import cadence
from agents.engagement.triggers import TriggerCandidate
from storage import db

logger = logging.getLogger(__name__)


async def send(text: str, candidate: TriggerCandidate,
               send_text_fn: Callable) -> int | None:
    """Send via the bridge's existing choreography path; write a
    proactive_events row. Returns the row id or None on failure.

    The bridge's send_text returns (final_text, telegram_message_id, sent).
    We persist the message id so Sprint 2's reaction-feedback join has a
    real key (the column is NULL otherwise and the future selector can't
    down-weight thumbs-downed sources)."""
    telegram_message_id: int | None = None
    try:
        result = await send_text_fn(text)
    except Exception:
        logger.exception("sender: send_text_fn failed")
        return None
    if isinstance(result, tuple) and len(result) >= 2:
        try:
            telegram_message_id = (
                int(result[1]) if result[1] is not None else None
            )
        except (TypeError, ValueError):
            telegram_message_id = None
    try:
        row_id = db.proactive_event_insert(
            source=candidate.source,
            pattern=candidate.pattern,
            payload_json=json.dumps(candidate.payload, default=str),
            telegram_message_id=telegram_message_id,
        )
    except Exception:
        logger.exception("sender: proactive_event_insert failed (non-fatal)")
        return None
    try:
        cadence.record_user_anchored_sent(candidate.source)
    except Exception:
        logger.exception("sender: record_user_anchored_sent failed (non-fatal)")
    return row_id
