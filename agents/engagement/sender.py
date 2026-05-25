"""Sender: wraps the send_text choreography function via the global
proactive gate (reserve_and_send) and writes one proactive_events row."""
from __future__ import annotations

import json
import logging
import re

from agents.proactive_gate import reserve_and_send

logger = logging.getLogger(__name__)

_DEFER_PATTERN = re.compile(r"\[\[defer:(next_turn|reflect)\]\]", re.IGNORECASE)


def _handle_defer(text: str, candidate) -> tuple[str | None, str]:
    """Detect [[defer:next_turn]] or [[defer:reflect]] in text.

    Returns (kind, clean_text) where kind is 'next_turn'|'reflect'|None.
    clean_text has the sentinel stripped.
    """
    m = _DEFER_PATTERN.search(text)
    if not m:
        return None, text
    kind = m.group(1).lower()
    clean = _DEFER_PATTERN.sub("", text).strip()
    return kind, clean


def _write_defer_scratch(kind: str, text: str, candidate) -> None:
    """Write a deferred proactive item to session_scratch for later surfacing."""
    from storage import db as _db
    session_id = _db.get_session_id() or "pending"
    payload = json.dumps({
        "source": candidate.source,
        "pattern": candidate.pattern,
        "text": text,
        "payload": getattr(candidate, "payload", {}),
    }, default=str)
    topic = f"defer:{kind}"
    try:
        with _db._conn() as conn:
            conn.execute(
                "INSERT INTO session_scratch (session_id, topic, payload_json) VALUES (?, ?, ?)",
                (session_id, topic, payload),
            )
    except Exception:
        logger.exception("sender: failed to write defer scratch for %s", candidate.source)


async def send(text, candidate, send_text_fn) -> int | None:
    """Send a proactive engagement candidate.

    Returns the proactive_events row id on a confirmed send, None when the
    gate suppressed the candidate (silence_window / quiet_hours / dedup /
    send_failed / empty_text / deferred). Scheduler must skip mark_consumed
    on None — otherwise producer sticky state would mark untouched triggers
    as 'handled'.
    """
    # Detect [[defer:next_turn]] or [[defer:reflect]] sentinel from the
    # proactive composer. Strip it from the text and write to session_scratch;
    # don't send this turn.
    defer_kind, text = _handle_defer(text or "", candidate)
    if defer_kind:
        _write_defer_scratch(defer_kind, text, candidate)
        logger.info("sender: deferred %s (%s) to session_scratch", candidate.source, defer_kind)
        return None

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
