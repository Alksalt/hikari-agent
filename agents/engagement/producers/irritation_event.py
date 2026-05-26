"""Producer: fires when hikari_world.minor_frustration is non-empty and
hasn't been surfaced today.

Surfaces the frustration in Hikari's voice once per day per frustration
string; marks it consumed via a runtime_state key containing the last
surfaced frustration hash + date so repeat-fires on the same irritant
within the same day are suppressed.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta

from agents import config as cfg
from agents.engagement.triggers import TriggerCandidate
from storage import db

logger = logging.getLogger(__name__)

_LAST_SURFACED_KEY = "engagement.irritation_event.last_surfaced"


def _frustration_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def collect() -> list[TriggerCandidate]:
    if not bool(cfg.get("engagement.irritation_event.enabled", True)):
        return []

    raw = db.get_core_block("hikari_world")
    if not raw:
        return []
    try:
        world = json.loads(raw)
    except (ValueError, TypeError):
        return []

    frustration = (world.get("minor_frustration") or "").strip()
    if not frustration:
        return []

    today = datetime.now(UTC).date().isoformat()
    fhash = _frustration_hash(frustration)
    last_surfaced = (db.runtime_get(_LAST_SURFACED_KEY) or "").strip()
    # Format stored: "YYYY-MM-DD:hash16"
    if last_surfaced == f"{today}:{fhash}":
        return []

    now = datetime.now(UTC)
    return [TriggerCandidate(
        source="irritation_event",
        pool="agent_spontaneous",
        pattern="notify",
        novelty=0.6,
        actionability=0.4,
        confidence=0.8,
        payload={"frustration": frustration[:200]},
        dedup_key=f"irritation_event:{fhash}:{today}",
        decay_at=now + timedelta(hours=6),
    )]


def mark_consumed(candidate: TriggerCandidate) -> None:
    frustration = candidate.payload.get("frustration") or ""
    if not frustration:
        return
    today = datetime.now(UTC).date().isoformat()
    fhash = _frustration_hash(frustration)
    db.runtime_set(_LAST_SURFACED_KEY, f"{today}:{fhash}")
