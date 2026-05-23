"""Producer: fires when the user arrives at a recurring-visit location (opt-in).

Wraps agents.proactive.detect_recurring_location_pattern. Returns [] when
no location data is available or the pattern threshold isn't met.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from agents import config as cfg
from agents.engagement.triggers import TriggerCandidate
from storage import db

logger = logging.getLogger(__name__)

_DEDUP_KEY = "engagement.location_arrived_recurring.last_place_key"


def collect() -> list[TriggerCandidate]:
    if not bool(cfg.get("engagement.location_arrived_recurring.enabled", False)):
        return []

    try:
        from agents.proactive import detect_recurring_location_pattern
        pattern = detect_recurring_location_pattern()
    except Exception:
        logger.exception("location_arrived_recurring: detect_recurring_location_pattern failed")
        return []

    if not pattern:
        return []

    lat = pattern.get("lat")
    lon = pattern.get("lon")
    label = pattern.get("label") or ""
    visit_count = int(pattern.get("visit_count") or 0)

    place_name = label if label else f"{lat:.3f},{lon:.3f}"
    place_key = f"{round(float(lat or 0), 3)},{round(float(lon or 0), 3)}"

    # Dedup: don't re-fire for the same cluster until it changes.
    last_key = db.runtime_get(_DEDUP_KEY)
    if last_key == place_key:
        return []

    now = datetime.now(UTC)
    return [TriggerCandidate(
        source="location_arrived_recurring",
        pool="user_anchored",
        pattern="notify",
        novelty=0.65,
        actionability=0.5,
        confidence=0.75,
        payload={
            "place_name": place_name,
            "visit_count": visit_count,
            "lat": lat,
            "lon": lon,
            "_place_key": place_key,
        },
        dedup_key=f"location_arrived_recurring:{place_key}",
        decay_at=now + timedelta(hours=4),
    )]


def mark_consumed(candidate: TriggerCandidate) -> None:
    place_key = candidate.payload.get("_place_key")
    if place_key:
        db.runtime_set(_DEDUP_KEY, str(place_key))
