"""Sender: wraps the send_text choreography function via the global
proactive gate (reserve_and_send) and writes one proactive_events row.

Public API additions (Wave 2):
  - send(): extended with outcome=defer_to_next_turn path that writes to
    db.runtime_set("deferred_observations", ...) for next-turn injection.
  - on_reaction(source_id, direction): called by the bridge on 👍/👎 reactions;
    updates proactive_source_scores EMA and thumbs counters.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

from agents.proactive_gate import reserve_and_send

logger = logging.getLogger(__name__)

_DEFER_PATTERN = re.compile(r"\[\[defer:(next_turn)\]\]", re.IGNORECASE)

# EMA alpha for on_reaction updates (higher = faster adaptation)
_EMA_ALPHA = 0.3


def _handle_defer(text: str, candidate) -> tuple[str | None, str]:
    """Detect [[defer:next_turn]] in text.

    Returns (kind, clean_text) where kind is 'next_turn'|None.
    clean_text has the sentinel stripped.
    """
    m = _DEFER_PATTERN.search(text)
    if not m:
        return None, text
    kind = m.group(1).lower()
    clean = _DEFER_PATTERN.sub("", text).strip()
    return kind, clean


def _write_defer_scratch(kind: str, text: str, candidate) -> bool:
    """Write a deferred proactive item to session_scratch AND deferred_observations.

    Returns True on success (both writes may partially fail; True if at least one succeeded).
    """
    from storage import db as _db
    session_id = _db.get_session_id() or "pending"
    payload = json.dumps({
        "source": candidate.source,
        "pattern": candidate.pattern,
        "text": text,
        "payload": getattr(candidate, "payload", {}),
    }, default=str)
    topic = f"defer:{kind}"
    scratch_ok = False
    try:
        with _db._conn() as conn:
            conn.execute(
                "INSERT INTO session_scratch (session_id, topic, payload_json) VALUES (?, ?, ?)",
                (session_id, topic, payload),
            )
        scratch_ok = True
    except Exception:
        logger.exception("sender: failed to write defer scratch for %s", candidate.source)

    # outcome=defer_to_next_turn: also write to deferred_observations runtime key
    # so the hooks-inject agent (Wave 2 #3) can pull this on the next user turn.
    if kind == "next_turn":
        try:
            now_iso = datetime.now(UTC).isoformat()
            # Merge with any existing deferred observations (list append)
            existing_raw = _db.runtime_get("deferred_observations")
            existing: list = json.loads(existing_raw) if existing_raw else []
            existing.append({"text": text, "ts": now_iso, "source": candidate.source})
            _db.runtime_set("deferred_observations", json.dumps(existing))
            logger.info(
                "sender: defer_to_next_turn — wrote observation for %s to deferred_observations",
                candidate.source,
            )
        except Exception:
            logger.exception(
                "sender: failed to write deferred_observations for %s (non-fatal)",
                candidate.source,
            )

    return scratch_ok


def on_reaction(source_id: str, direction: str) -> None:
    """Update proactive_source_scores when the user reacts with 👍 or 👎.

    Called by the bridge on a thumbs-up/down telegram reaction.

    direction: "up" (thumbs-up) or "down" (thumbs-down).
    Adjusts the EMA toward 1.0 for up, 0.0 for down.
    """
    from storage import db as _db

    thumbs_up = 1 if direction == "up" else 0
    thumbs_down = 1 if direction == "down" else 0

    # Read the current EMA so we can adjust it.
    try:
        rows = _db.proactive_source_scores_all()
        current_ema: float = 0.5  # default if no row yet
        for row in rows:
            if row.get("source") == source_id:
                current_ema = float(row.get("ema") or 0.5)
                break

        target = 1.0 if direction == "up" else 0.0
        new_ema = current_ema + _EMA_ALPHA * (target - current_ema)

        _db.proactive_source_score_upsert(
            source_id,
            ema=new_ema,
            thumbs_up=thumbs_up,
            thumbs_down=thumbs_down,
        )
        logger.info(
            "on_reaction: %s direction=%s ema %.3f → %.3f",
            source_id, direction, current_ema, new_ema,
        )
    except Exception:
        logger.exception("on_reaction: failed to update source score for %s", source_id)


async def send(text, candidate, send_text_fn) -> int | None:
    """Send a proactive engagement candidate.

    Returns the proactive_events row id on a confirmed send, None when the
    gate suppressed the candidate (silence_window / quiet_hours / dedup /
    send_failed / empty_text / deferred). Scheduler must skip mark_consumed
    on None — otherwise producer sticky state would mark untouched triggers
    as 'handled'.
    """
    # Detect [[defer:next_turn]] sentinel from the proactive composer.
    # Strip it from the text and write to session_scratch; don't send this turn.
    # On scratch write failure, fall through to send immediately.
    defer_kind, text = _handle_defer(text or "", candidate)
    if defer_kind:
        if _write_defer_scratch(defer_kind, text, candidate):
            logger.info("sender: deferred %s (%s) to session_scratch", candidate.source, defer_kind)
            return None
        logger.warning(
            "sender: defer scratch failed for %s — falling through to send",
            candidate.source,
        )

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
        pool = cadence._resolve_pool(candidate.source)
        if pool is cadence.Pool.AGENT_SPONTANEOUS:
            cadence.record_spontaneous_sent(candidate.source)
        elif pool is cadence.Pool.SCHEDULED_CEREMONY:
            cadence.record_ceremony_sent(candidate.source)
        else:
            cadence.record_user_anchored_sent(candidate.source)
    except Exception:
        logger.exception("sender: cadence record failed (non-fatal)")
    # cofire commit — D5 exposes selector.commit_cofire(source); call it after
    # a confirmed send so cofire state is only persisted when a row id exists.
    try:
        from agents.engagement import selector as _selector
        _selector.commit_cofire(candidate.source)
    except AttributeError:
        # D5 not yet landed — commit_cofire not present; harmless until it ships
        pass
    except Exception:
        logger.exception("sender: commit_cofire failed (non-fatal)")
    return result.event_id
