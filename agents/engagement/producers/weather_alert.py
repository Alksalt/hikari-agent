"""Producer: fires on notable weather threshold exceedance (opt-in).

Reads from runtime_state key written by morning_brief or location weather
fetch. Returns [] when no fresh weather data is available.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from agents import config as cfg
from agents.engagement.triggers import TriggerCandidate
from storage import db

logger = logging.getLogger(__name__)

_DEDUP_KEY = "engagement.weather_alert.last_alert_summary"


def collect() -> list[TriggerCandidate]:
    if not bool(cfg.get("engagement.weather_alert.enabled", False)):
        return []

    # Weather alert written by morning_brief / location fetch:
    # {"alert_summary": "...", "wind_kmh": N, "precip_mm": N}
    raw = db.runtime_get("weather_alert_pending")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []

    alert_summary = str(data.get("alert_summary") or "").strip()
    if not alert_summary:
        return []

    # Dedup: skip if we already notified this exact summary.
    last_summary = db.runtime_get(_DEDUP_KEY)
    if last_summary == alert_summary:
        return []

    now = datetime.now(UTC)
    return [TriggerCandidate(
        source="weather_alert",
        pool="user_anchored",
        pattern="notify",
        novelty=0.8,
        actionability=0.7,
        confidence=0.85,
        payload={"alert_summary": alert_summary},
        dedup_key=f"weather_alert:{alert_summary[:64]}",
        decay_at=now + timedelta(hours=3),
    )]


def mark_consumed(candidate: TriggerCandidate) -> None:
    summary = candidate.payload.get("alert_summary")
    if summary:
        db.runtime_set(_DEDUP_KEY, str(summary))
