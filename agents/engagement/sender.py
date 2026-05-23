"""Sender: wraps the send_text choreography function via the global
proactive gate (reserve_and_send) and writes one proactive_events row."""
from __future__ import annotations

import json
import logging

from agents.proactive_gate import reserve_and_send

logger = logging.getLogger(__name__)


async def send(text, candidate, send_text_fn) -> int | None:
    """Send a proactive engagement candidate.

    Returns the proactive_events row id on a confirmed send, None when the
    gate suppressed the candidate (silence_window / quiet_hours / dedup /
    send_failed / empty_text). Scheduler must skip mark_consumed on None —
    otherwise producer sticky state (reengage_sent_for_gap, _DEDUP_KEY,
    watermarks) would mark untouched triggers as 'handled'.
    """
    payload = json.dumps(getattr(candidate, "payload", {}) or {}, default=str)
    result = await reserve_and_send(
        send_text_fn=send_text_fn,
        producer_id=candidate.source,
        pattern=candidate.pattern,
        text=text,
        payload_json=payload,
        dedup_key=candidate.dedup_key,
    )
    if result.status != "sent":
        logger.info("sender: gate aborted (%s) for %s", result.reason, candidate.source)
        return None
    from agents import cadence
    try:
        cadence.record_user_anchored_sent(candidate.source)
    except Exception:
        logger.exception("sender: record_user_anchored_sent failed (non-fatal)")
    return result.event_id
