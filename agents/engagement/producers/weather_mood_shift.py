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
from zoneinfo import ZoneInfo

from agents import config as cfg
from agents.engagement.triggers import TriggerCandidate
from agents.hooks import _resolve_local_tz_name
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


def _local_hour() -> int:
    """Current hour in the user's local tz, not the host/UTC clock.

    The window boundaries below (morning/midday/evening) are local-time
    concepts — reading datetime.now(UTC).hour against them picks the wrong
    window for any non-UTC user, flipping the tag with no real weather change.
    """
    try:
        return datetime.now(ZoneInfo(_resolve_local_tz_name())).hour
    except Exception:
        return datetime.now(UTC).hour


def _condition_tag(data: dict, now_local_hour: int) -> tuple[str | None, list]:
    """Derive a coarse condition tag from a weather snapshot dict.

    Returns ``(tag, fingerprint)`` where ``fingerprint`` is the raw
    ``[weather_code, temp_c]`` pair the tag was derived from — callers use it
    to gate emission on an actual data change, not just a window-slot flip.
    """
    windows = data.get("windows") or {}
    consensus = data.get("consensus") or {}

    # Pick the relevant window slot.
    if _EVENING_HOUR_START <= now_local_hour <= _EVENING_HOUR_END:
        window = windows.get("evening") or {}
    elif 7 <= now_local_hour < 12:
        window = windows.get("morning") or {}
    else:
        window = windows.get("midday") or {}

    code = window.get("weather_code")
    temp_c = window.get("temp_c") or consensus.get("temp_high_c")
    fingerprint = [code, round(float(temp_c), 1) if temp_c is not None else None]

    if code is not None:
        try:
            wmo = int(code)
        except (ValueError, TypeError):
            wmo = -1
        if wmo in _RAIN_CODES:
            # Distinguish evening rain from daytime rain for the trigger text.
            if _EVENING_HOUR_START <= now_local_hour <= _EVENING_HOUR_END:
                return "rain_evening", fingerprint
            return "rain", fingerprint

    if temp_c is not None:
        try:
            t = float(temp_c)
        except (ValueError, TypeError):
            t = None
        if t is not None:
            if t >= _HOT_THRESHOLD_C:
                return "hot", fingerprint
            if t <= _COLD_SNAP_THRESHOLD_C:
                return "cold_snap", fingerprint

    return None, fingerprint


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

    tag, fingerprint = _condition_tag(snapshot, _local_hour())
    if not tag:
        return []

    last_tag: str | None = None
    last_fingerprint: list | None = None
    stored_raw = (db.runtime_get(_LAST_CONDITION_KEY) or "").strip()
    if stored_raw:
        try:
            stored = json.loads(stored_raw)
        except (ValueError, TypeError):
            stored = None
        if isinstance(stored, dict):
            last_tag = stored.get("tag")
            last_fingerprint = stored.get("fingerprint")
        else:
            # Legacy plain-string tag written before the fingerprint gate.
            last_tag = stored_raw

    # First observation — record it but don't fire yet.
    if not last_tag:
        db.runtime_set(
            _LAST_CONDITION_KEY,
            json.dumps({"tag": tag, "fingerprint": fingerprint}),
        )
        return []

    # No transition — same condition as last time.
    if tag == last_tag:
        return []

    # A label flip with the exact same underlying code/temp is a window-slot
    # artifact (e.g. a different bucket read the same unchanged forecast),
    # not a real weather change — don't fire on it.
    if last_fingerprint is not None and fingerprint == last_fingerprint:
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
            "_fingerprint": fingerprint,
            "snapshot_summary": str(snapshot.get("summary") or "")[:120],
        },
        dedup_key=f"weather_mood_shift:{last_tag}→{tag}:{now.date().isoformat()}",
        decay_at=now + timedelta(hours=5),
    )]


def mark_consumed(candidate: TriggerCandidate) -> None:
    to_condition = candidate.payload.get("to_condition")
    if to_condition:
        db.runtime_set(
            _LAST_CONDITION_KEY,
            json.dumps({
                "tag": str(to_condition),
                "fingerprint": candidate.payload.get("_fingerprint"),
            }),
        )
