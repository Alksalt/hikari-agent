"""Producer: fires once per night when time_texture is "deep_night" AND
mutual silence has exceeded 4 hours.

The denial layer is allowed to drop in this message — lower guard, more
direct. Fires at most once per calendar night; deduped via runtime_state
key storing the last fire date (UTC).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from agents import config as cfg
from agents.engagement.triggers import TriggerCandidate
from storage import db

logger = logging.getLogger(__name__)

_LAST_FIRE_DATE_KEY = "engagement.late_night_dissolution.last_fire_date"
_MIN_SILENCE_HOURS = 4.0


def _parse_dt(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(iso)
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def collect() -> list[TriggerCandidate]:
    if not bool(cfg.get("engagement.late_night_dissolution.enabled", True)):
        return []

    # Stage gate: don't fire before the relationship has matured enough.
    stage = db.runtime_get_int("relationship_stage", 1)
    min_stage = int(cfg.get("engagement.late_night_dissolution.min_stage", 6))
    if stage < min_stage:
        logger.debug("late_night_dissolution: stage %d < min_stage %d — skipping", stage, min_stage)
        return []

    texture = (db.runtime_get("time_texture") or "").strip().lower()
    if texture != "deep_night":
        return []

    # Dedup: only once per calendar night.
    today = datetime.now(UTC).date().isoformat()
    last_fire_date = (db.runtime_get(_LAST_FIRE_DATE_KEY) or "").strip()
    if last_fire_date == today:
        return []

    # Silence gate: last inbound user message must be > 4h ago.
    now = datetime.now(UTC)
    last_user_msg = _parse_dt(db.runtime_get("last_user_message"))
    if not last_user_msg:
        return []
    elapsed_h = (now - last_user_msg).total_seconds() / 3600
    if elapsed_h < _MIN_SILENCE_HOURS:
        return []

    return [TriggerCandidate(
        source="late_night_dissolution",
        pool="agent_spontaneous",
        pattern="notify",
        novelty=0.65,
        actionability=0.3,
        confidence=0.75,
        payload={
            "elapsed_hours": round(elapsed_h, 1),
            "last_message_ts": last_user_msg.isoformat(),
        },
        dedup_key=f"late_night_dissolution:{today}",
        decay_at=now + timedelta(hours=4),
    )]


def mark_consumed(candidate: TriggerCandidate) -> None:
    today = datetime.now(UTC).date().isoformat()
    db.runtime_set(_LAST_FIRE_DATE_KEY, today)
