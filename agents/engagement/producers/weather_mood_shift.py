"""Producer: fires on notable weather transitions — rain-just-started in the
evening, first hot day of a streak, or cold snap.

Reads from runtime_state key "weather_current_snapshot" (JSON written by
morning_brief or the weather_fetch tool handler). Detects transitions by
comparing to the previously stored condition snapshot. Fires once per
transition, not on steady-state.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from agents import config as cfg
from agents.engagement.triggers import TriggerCandidate
from storage import db

logger = logging.getLogger(__name__)

_LAST_CONDITION_KEY = "engagement.weather_mood_shift.last_condition_tag"

# Weather code ranges for rain and clear/hot detection (WMO codes).
# 51-67: drizzle + rain; 80-82: showers; 95-99: thunderstorm.
_RAIN_CODES = frozenset(range(51, 68)) | frozenset(range(80, 83)) | frozenset(range(95, 100))
_HOT_THRESHOLD_C = 28.0
_COLD_SNAP_THRESHOLD_C = 5.0

_EVENING_HOUR_START = 17
_EVENING_HOUR_END = 22


def _condition_tag(data: dict) -> str | None:
    """Derive a coarse condition tag from a weather snapshot dict."""
    windows = data.get("windows") or {}
    consensus = data.get("consensus") or {}

    now_utc_hour = datetime.now(UTC).hour
    # Pick the relevant window slot.
    if _EVENING_HOUR_START <= now_utc_hour <= _EVENING_HOUR_END:
        window = windows.get("evening") or {}
    elif 7 <= now_utc_hour < 12:
        window = windows.get("morning") or {}
    else:
        window = windows.get("midday") or {}

    code = window.get("weather_code")
    temp_c = window.get("temp_c") or consensus.get("temp_high_c")

    if code is not None:
        try:
            wmo = int(code)
        except (ValueError, TypeError):
            wmo = -1
        if wmo in _RAIN_CODES:
            # Distinguish evening rain from daytime rain for the trigger text.
            if _EVENING_HOUR_START <= now_utc_hour <= _EVENING_HOUR_END:
                return "rain_evening"
            return "rain"

    if temp_c is not None:
        try:
            t = float(temp_c)
        except (ValueError, TypeError):
            t = None
        if t is not None:
            if t >= _HOT_THRESHOLD_C:
                return "hot"
            if t <= _COLD_SNAP_THRESHOLD_C:
                return "cold_snap"

    return None


def collect() -> list[TriggerCandidate]:
    if not bool(cfg.get("engagement.weather_mood_shift.enabled", True)):
        return []

    # Try the snapshot written by weather_fetch / morning_brief.
    raw = db.runtime_get("weather_current_snapshot")
    if not raw:
        return []
    try:
        snapshot = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(snapshot, dict):
        return []

    tag = _condition_tag(snapshot)
    if not tag:
        return []

    last_tag = (db.runtime_get(_LAST_CONDITION_KEY) or "").strip()

    # No transition — same condition as last time.
    if tag == last_tag:
        return []

    # First observation — record it but don't fire yet.
    if not last_tag:
        db.runtime_set(_LAST_CONDITION_KEY, tag)
        return []

    # Transition detected.
    now = datetime.now(UTC)
    return [TriggerCandidate(
        source="weather_mood_shift",
        pool="agent_spontaneous",
        pattern="notify",
        novelty=0.65,
        actionability=0.35,
        confidence=0.7,
        payload={
            "from_condition": last_tag,
            "to_condition": tag,
            "snapshot_summary": str(snapshot.get("summary") or "")[:120],
        },
        dedup_key=f"weather_mood_shift:{last_tag}→{tag}:{now.date().isoformat()}",
        decay_at=now + timedelta(hours=5),
    )]


def mark_consumed(candidate: TriggerCandidate) -> None:
    to_condition = candidate.payload.get("to_condition")
    if to_condition:
        db.runtime_set(_LAST_CONDITION_KEY, str(to_condition))
