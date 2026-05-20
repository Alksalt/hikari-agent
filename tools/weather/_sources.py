"""Per-source weather fetchers + consensus aggregator.

Three free sources: open-meteo (no key), met.no/yr (no key, attribution
via User-Agent), and OpenWeatherMap (optional, key-gated). Each
fetcher returns a small dict or ``None`` on failure — failures are
logged at exception level but never propagate, so the aggregator can
fall back to whichever sources did respond.

``httpx`` is imported INSIDE each fetcher (not at module top) so
``tools/weather`` stays a cheap import for the registry: the network
dep doesn't load until someone actually requests a forecast.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from tools.weather._shared import (
    _MET_NO_URL,
    _MET_NO_USER_AGENT,
    _OPEN_METEO_URL,
    _OPENWEATHERMAP_URL,
)

logger = logging.getLogger(__name__)


async def _fetch_open_meteo(lat: float, lon: float) -> dict[str, Any] | None:
    import httpx  # noqa: PLC0415 — lazy: keep registry import cheap
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                _OPEN_METEO_URL,
                params={
                    "latitude": lat, "longitude": lon,
                    "daily": (
                        "temperature_2m_max,temperature_2m_min,"
                        "weather_code,precipitation_probability_max"
                    ),
                    "forecast_days": 1,
                    "timezone": "auto",
                },
            )
            r.raise_for_status()
            d = (r.json() or {}).get("daily") or {}
            if not d.get("time"):
                return None
            return {
                "temp_high_c": d["temperature_2m_max"][0],
                "temp_low_c": d["temperature_2m_min"][0],
                "weather_code": d["weather_code"][0],
                "precip_prob_max": d["precipitation_probability_max"][0],
                "date": d["time"][0],
            }
    except Exception:
        logger.exception("open-meteo fetch failed")
        return None


async def _fetch_met_no(lat: float, lon: float) -> dict[str, Any] | None:
    """met.no — Norwegian Met Office. Requires User-Agent attribution."""
    import httpx  # noqa: PLC0415 — lazy: keep registry import cheap
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                _MET_NO_URL,
                params={"lat": lat, "lon": lon},
                headers={"User-Agent": _MET_NO_USER_AGENT},
            )
            r.raise_for_status()
            ts = ((r.json() or {}).get("properties") or {}).get("timeseries") or []
            if not ts:
                return None
            from datetime import UTC, datetime, timedelta
            now = datetime.now(UTC)
            cutoff = now + timedelta(hours=24)
            temps: list[float] = []
            symbols: list[str] = []
            for point in ts:
                try:
                    t = datetime.fromisoformat(point["time"].replace("Z", "+00:00"))
                except (KeyError, ValueError):
                    continue
                if t > cutoff:
                    break
                details = (point.get("data") or {}).get("instant", {}).get("details") or {}
                if "air_temperature" in details:
                    temps.append(float(details["air_temperature"]))
                n6 = (point.get("data") or {}).get("next_6_hours") or {}
                sym = ((n6.get("summary") or {}).get("symbol_code")) or ""
                if sym:
                    symbols.append(sym)
            if not temps:
                return None
            return {
                "temp_high_c": max(temps),
                "temp_low_c": min(temps),
                "symbol": symbols[0] if symbols else None,
            }
    except Exception:
        logger.exception("met.no fetch failed")
        return None


async def _fetch_openweathermap(lat: float, lon: float) -> dict[str, Any] | None:
    api_key = os.environ.get("OPENWEATHERMAP_API_KEY")
    if not api_key:
        return None
    import httpx  # noqa: PLC0415 — lazy: keep registry import cheap
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                _OPENWEATHERMAP_URL,
                params={"lat": lat, "lon": lon, "appid": api_key, "units": "metric"},
            )
            r.raise_for_status()
            items = (r.json() or {}).get("list") or []
            if not items:
                return None
            from datetime import UTC, datetime, timedelta
            cutoff = datetime.now(UTC) + timedelta(hours=24)
            temps = []
            for it in items:
                try:
                    t = datetime.fromtimestamp(it["dt"], tz=UTC)
                except (KeyError, ValueError):
                    continue
                if t > cutoff:
                    break
                temps.append(float(((it.get("main") or {}).get("temp")) or 0))
            if not temps:
                return None
            return {"temp_high_c": max(temps), "temp_low_c": min(temps)}
    except Exception:
        logger.exception("openweathermap fetch failed")
        return None


def _consensus(sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not sources:
        return {}
    highs = [s["temp_high_c"] for s in sources.values() if s.get("temp_high_c") is not None]
    lows  = [s["temp_low_c"]  for s in sources.values() if s.get("temp_low_c")  is not None]
    return {
        "temp_high_c": round(sum(highs) / len(highs), 1) if highs else None,
        "temp_low_c": round(sum(lows) / len(lows), 1) if lows else None,
        "high_spread": (max(highs) - min(highs)) if len(highs) > 1 else 0,
    }
