"""Producer: surfaces a high-importance past episode as a callback nudge (opt-in).

Delegates to agents.callback_surface.pick_callback_candidate with an empty
query so it picks the highest-importance recent episode regardless of topic.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from agents import config as cfg
from agents.engagement.triggers import TriggerCandidate
from storage import db

logger = logging.getLogger(__name__)

_DEDUP_KEY = "engagement.callback_episode.last_surfaced_id"


def collect() -> list[TriggerCandidate]:
    if not bool(cfg.get("engagement.callback_episode.enabled", False)):
        return []

    # Use a broad query so pick_callback_candidate can score by importance.
    try:
        from agents.callback_surface import pick_callback_candidate
        candidate = pick_callback_candidate("")  # empty query → importance-ranked
    except Exception:
        logger.exception("callback_episode: pick_callback_candidate failed")
        return []

    if not candidate:
        return []

    cid = str(candidate.get("id") or "")
    text = str(candidate.get("text") or "").strip()
    date = str(candidate.get("date") or "")
    if not text:
        return []

    # Dedup: don't re-surface the same episode in back-to-back ticks.
    last_id = db.runtime_get(_DEDUP_KEY)
    if last_id == cid:
        return []

    now = datetime.now(UTC)
    return [TriggerCandidate(
        source="callback_episode",
        pool="agent_spontaneous",
        pattern="notify",
        novelty=float(candidate.get("score", 0.3)),
        actionability=0.5,
        confidence=0.7,
        payload={"text": text[:400], "date": date, "id": cid},
        dedup_key=f"callback_episode:{cid}",
        decay_at=now + timedelta(hours=6),
    )]


def mark_consumed(candidate: TriggerCandidate) -> None:
    cid = candidate.payload.get("id")
    if cid:
        db.runtime_set(_DEDUP_KEY, str(cid))
