"""Consolidated daily brief (Sprint 1) — ONE payload-carrying morning digest.

Replaces morning_brief (unconditional 06:00 weather) and daily_checkin
(07:00 permission-ask). Send-iff rule: sections without signal are omitted;
a fully-empty brief is not sent. See DECISIONS.md + the 2026-07-02 spec.

This module currently holds the data half — section collectors + the
weather notability gate (Task 2). The composer + orchestrator (Task 3)
grow this file; imports for that half (scheduling, voice composition,
untrusted-content wrapping) land there, not here, so this module stays
ruff-clean with no dead imports in the meantime.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from agents import config as cfg
from agents.daily_checkin import fetch_calendar_events, fetch_email_buckets
from agents.morning_brief import _resolve_location
from storage import db
from tools.weather import fetch_forecast

logger = logging.getLogger(__name__)

_SNAPSHOT_KEY = "weather_current_snapshot"


def _c(key: str, default):
    return cfg.get(f"daily_brief.{key}", default)


# ---------- weather notability ----------

def _consensus(forecast: dict | None) -> dict:
    if not isinstance(forecast, dict):
        return {}
    return (forecast.get("consensus") or {}).get("values") or {}


def _is_wet(forecast: dict, rain_threshold: float) -> bool:
    rain = _consensus(forecast).get("precip_prob_max_pct")
    return rain is not None and float(rain) >= rain_threshold


def _weather_notable(forecast: dict, prev: dict | None) -> tuple[bool, list[str]]:
    """Weather earns a brief slot iff actionable or changed vs yesterday."""
    rain_thr = float(_c("weather_rain_prob_threshold_pct", 40))
    temp_thr = float(_c("weather_temp_delta_threshold_c", 5))
    wind_thr = float(_c("weather_wind_threshold_kmh", 40))
    reasons: list[str] = []
    if prev is None:
        return True, ["no baseline yet"]
    cur, old = _consensus(forecast), _consensus(prev)
    rain = cur.get("precip_prob_max_pct")
    if rain is not None and float(rain) >= rain_thr:
        reasons.append(f"rain {rain}%")
    high, prev_high = cur.get("temp_high_c"), old.get("temp_high_c")
    if (high is not None and prev_high is not None
            and abs(float(high) - float(prev_high)) >= temp_thr):
        reasons.append(f"temp delta {float(high) - float(prev_high):+.0f}°")
    wind = cur.get("wind_max_kmh")
    if wind is not None and float(wind) >= wind_thr:
        reasons.append(f"wind {wind} km/h")
    if _is_wet(forecast, rain_thr) != _is_wet(prev, rain_thr):
        reasons.append("wet/dry flip vs yesterday")
    return bool(reasons), reasons


async def _collect_weather() -> dict[str, Any] | None:
    loc = _resolve_location()
    if loc is None:
        return None
    lat, lon, label = loc
    try:
        forecast = await fetch_forecast(lat, lon)
    except Exception:
        logger.exception("daily_brief: fetch_forecast failed")
        return None
    if not forecast.get("sources"):
        return None
    prev_raw = db.runtime_get(_SNAPSHOT_KEY)
    prev = None
    if prev_raw:
        try:
            prev = json.loads(prev_raw)
        except (ValueError, TypeError):
            prev = None
    # Keep feeding weather_mood_shift regardless of notability.
    try:
        db.runtime_set(_SNAPSHOT_KEY, json.dumps(forecast))
    except Exception:
        logger.exception("daily_brief: snapshot write failed (non-fatal)")
    notable, reasons = _weather_notable(forecast, prev)
    if not notable:
        logger.info("daily_brief: weather not notable — omitted")
        return None
    return {"forecast": forecast, "label": label, "reasons": reasons}


async def collect_sections() -> dict[str, Any]:
    """Gather all sections. None = no signal = omitted from the brief."""
    weather = await _collect_weather()
    email = await fetch_email_buckets()
    if not (email.get("unread_personal") or email.get("calendar_invites")
            or int((email.get("deletable") or {}).get("count") or 0) > 0):
        email = None
    calendar = await fetch_calendar_events()
    if not calendar:
        calendar = None
    return {"weather": weather, "email": email, "calendar": calendar}
