"""Producer: fires once when mood_today == 'weirdly good' (opt-in).

Gate: the warmth budget — tracked via runtime_state so we don't fire more
than once per mood-day. No payload anchor required; the guard skips it for
this source.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from agents import config as cfg
from agents.engagement.triggers import TriggerCandidate
from storage import db

logger = logging.getLogger(__name__)

_DEDUP_KEY = "engagement.weirdly_good_mood_leak.last_fire_date"


def collect() -> list[TriggerCandidate]:
    if not bool(cfg.get("engagement.weirdly_good_mood_leak.enabled", False)):
        return []

    mood = (db.get_core_block("mood_today") or "focused").strip().lower()
    if mood != "weirdly good":
        return []

    # Only fire once per calendar day so we don't spam.
    today = datetime.now(UTC).date().isoformat()
    last_fire = db.runtime_get(_DEDUP_KEY)
    if last_fire == today:
        return []

    now = datetime.now(UTC)
    return [TriggerCandidate(
        source="weirdly_good_mood_leak",
        pool="agent_spontaneous",
        pattern="notify",
        novelty=0.6,
        actionability=0.4,
        confidence=0.7,
        payload={"mood": mood},
        dedup_key=f"weirdly_good_mood_leak:{today}",
        decay_at=now + timedelta(hours=6),
    )]


def mark_consumed(candidate: TriggerCandidate) -> None:
    today = datetime.now(UTC).date().isoformat()
    db.runtime_set(_DEDUP_KEY, today)
