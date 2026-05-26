"""Producer: fires when hikari_world.location transitions to "home",
the user has been silent for more than 1 hour, and the current local
hour is in the 17-23 range (evening return window).

Transition signal: hikari_world.location changed from non-home → "home".
Fires once per home-arrival event; marked consumed via runtime_state.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from agents import config as cfg
from agents.engagement.triggers import TriggerCandidate
from storage import db

logger = logging.getLogger(__name__)

_LAST_SEEN_LOCATION_KEY = "engagement.just_got_home.last_seen_location"
_LAST_FIRE_KEY = "engagement.just_got_home.last_fire_ts"
_MIN_SILENCE_HOURS = 1.0
_EVENING_HOUR_START = 17
_EVENING_HOUR_END = 23  # inclusive


def _parse_dt(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(iso)
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def collect() -> list[TriggerCandidate]:
    if not bool(cfg.get("engagement.just_got_home.enabled", True)):
        return []

    raw = db.get_core_block("hikari_world")
    if not raw:
        return []
    try:
        world = json.loads(raw)
    except (ValueError, TypeError):
        return []

    current_location = (world.get("location") or "").strip().lower()
    last_seen_location = (db.runtime_get(_LAST_SEEN_LOCATION_KEY) or "").strip().lower()

    # Update stored location if it changed, regardless of firing.
    if current_location != last_seen_location:
        db.runtime_set(_LAST_SEEN_LOCATION_KEY, current_location)

    # Only fire on a transition TO home from a different location.
    if current_location != "home":
        return []
    if last_seen_location == "home" or not last_seen_location:
        # Already at home or first observation — not a transition.
        return []

    # Time gate: only during evening hours (UTC approximation; good enough
    # given Hikari's persona; caller can offset via tz config if needed).
    now = datetime.now(UTC)
    local_hour = now.hour  # best approximation without tz config
    if not (_EVENING_HOUR_START <= local_hour <= _EVENING_HOUR_END):
        return []

    # Silence gate: user must have been quiet > 1h.
    last_user_msg = _parse_dt(db.runtime_get("last_user_message"))
    if last_user_msg:
        elapsed_h = (now - last_user_msg).total_seconds() / 3600
        if elapsed_h < _MIN_SILENCE_HOURS:
            return []

    # Dedup: don't fire twice for the same arrival event.
    last_fire = _parse_dt(db.runtime_get(_LAST_FIRE_KEY))
    if last_fire and (now - last_fire).total_seconds() < 3600 * 4:
        return []

    return [TriggerCandidate(
        source="just_got_home",
        pool="agent_spontaneous",
        pattern="notify",
        novelty=0.7,
        actionability=0.45,
        confidence=0.75,
        payload={"from_location": last_seen_location, "arrived_at": now.isoformat()},
        dedup_key=f"just_got_home:{now.date().isoformat()}",
        decay_at=now + timedelta(hours=3),
    )]


def mark_consumed(candidate: TriggerCandidate) -> None:
    db.runtime_set(_LAST_FIRE_KEY, datetime.now(UTC).isoformat())
