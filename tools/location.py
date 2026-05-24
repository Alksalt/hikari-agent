"""Location-in: receive user-shared location, reverse-geocode + fetch weather,
store as a fresh transient fact in ``runtime_state`` for the hook to inject.

The user must share location explicitly via Telegram. We never pull location
passively. Per the engagement research, Hikari should NOT mention location
knowledge in the FIRST message after a share — that reads creepy. So this
module stores a `defer_until_counter` value that the hook respects.

Endpoints (config-driven):
  - reverse_geocode_endpoint: Nominatim by default
  - weather_endpoint: Open-Meteo by default

Both are public, free, no auth needed.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from agents import config as cfg
from storage import db
from storage.db import INBOUND_MSG_COUNTER_KEY

logger = logging.getLogger(__name__)

_STATE_KEY = "user_location_state"


def _enabled() -> bool:
    return bool(cfg.get("location.enabled", True))


def _freshness_hours() -> float:
    return float(cfg.get("location.freshness_hours", 6.0))


def _defer_callback_turns() -> int:
    return int(cfg.get("location.defer_callback_turns", 1))


def _reverse_endpoint() -> str:
    return str(cfg.get(
        "location.reverse_geocode_endpoint",
        "https://nominatim.openstreetmap.org/reverse",
    ))


def _weather_endpoint() -> str:
    return str(cfg.get(
        "location.weather_endpoint",
        "https://api.open-meteo.com/v1/forecast",
    ))


def _user_agent() -> str:
    return str(cfg.get("location.nominatim_user_agent", "hikari-agent/0.1 (contact: hikari-bot@localhost)"))


async def record_share(lat: float, lon: float) -> dict[str, Any]:
    """Reverse-geocode + fetch weather, store in runtime_state, return the
    state dict. Idempotent — overwrites prior share."""
    if not _enabled():
        return {}
    label: str | None = None
    weather_short: str | None = None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                _reverse_endpoint(),
                params={"lat": lat, "lon": lon, "format": "jsonv2"},
                headers={"User-Agent": _user_agent()},
            )
            if r.status_code == 200:
                data = r.json() or {}
                addr = data.get("address") or {}
                label = (
                    addr.get("city")
                    or addr.get("town")
                    or addr.get("village")
                    or addr.get("suburb")
                    or addr.get("county")
                    or data.get("display_name")
                )
            else:
                logger.warning("location: nominatim HTTP %s", r.status_code)
    except Exception:
        logger.exception("location: reverse-geocode failed (non-fatal)")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                _weather_endpoint(),
                params={
                    "latitude": lat, "longitude": lon,
                    "current": "temperature_2m,weather_code,wind_speed_10m",
                },
            )
            if r.status_code == 200:
                w = (r.json() or {}).get("current") or {}
                temp = w.get("temperature_2m")
                wind = w.get("wind_speed_10m")
                if temp is not None:
                    weather_short = f"{temp:.0f}°C"
                    if wind is not None:
                        weather_short += f", wind {wind:.0f} km/h"
            else:
                logger.warning("location: open-meteo HTTP %s", r.status_code)
    except Exception:
        logger.exception("location: weather fetch failed (non-fatal)")

    # Defer mention by N turns to avoid creepy-immediate callback.
    inbound = db.runtime_get_int(INBOUND_MSG_COUNTER_KEY, 0)
    state = {
        "lat": lat,
        "lon": lon,
        "label": label,
        "weather": weather_short,
        "shared_at": datetime.now(UTC).isoformat(),
        "defer_until_counter": inbound + _defer_callback_turns(),
    }
    db.runtime_set(_STATE_KEY, json.dumps(state))
    return state


def current_location() -> dict[str, Any] | None:
    """Return the stored location IF fresh AND no-longer-deferred, else None."""
    if not _enabled():
        return None
    raw = db.runtime_get(_STATE_KEY)
    if not raw:
        return None
    try:
        state = json.loads(raw)
    except (ValueError, TypeError):
        return None
    try:
        shared_at = datetime.fromisoformat(state["shared_at"])
        if shared_at.tzinfo is None:
            shared_at = shared_at.replace(tzinfo=UTC)
    except (KeyError, ValueError, TypeError):
        return None
    if (datetime.now(UTC) - shared_at) > timedelta(hours=_freshness_hours()):
        # Stale; clear.
        db.runtime_set(_STATE_KEY, None)
        return None
    defer_until = int(state.get("defer_until_counter") or 0)
    current_counter = db.runtime_get_int(INBOUND_MSG_COUNTER_KEY, 0)
    if current_counter < defer_until:
        return None  # don't surface yet
    return state


def format_for_injection() -> str:
    state = current_location()
    if not state:
        return ""
    parts = []
    label = state.get("label")
    weather = state.get("weather")
    if label:
        parts.append(f"they're near: {label}")
    if weather:
        parts.append(f"weather there: {weather}")
    if not parts:
        return ""
    return (
        "# their location (they shared it; mention only if naturally relevant)\n"
        + "\n".join(f"- {p}" for p in parts)
    )
