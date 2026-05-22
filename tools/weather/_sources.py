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
import statistics
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
        daily = (
            "temperature_2m_max,temperature_2m_min,"
            "apparent_temperature_max,apparent_temperature_min,"
            "precipitation_probability_max,weather_code,"
            "wind_speed_10m_max,uv_index_max,sunrise,sunset"
        )
        hourly = (
            "temperature_2m,apparent_temperature,"
            "precipitation_probability,weather_code,cloud_cover"
        )
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                _OPEN_METEO_URL,
                params={
                    "latitude": lat, "longitude": lon,
                    "daily": daily,
                    "hourly": hourly,
                    "forecast_days": 1,
                    "timezone": "auto",
                },
            )
            r.raise_for_status()
            body = r.json() or {}
            d = body.get("daily") or {}
            h = body.get("hourly") or {}
            if not d.get("time"):
                return None
            return {
                "temp_high_c": d["temperature_2m_max"][0],
                "temp_low_c": d["temperature_2m_min"][0],
                "feels_high_c": (d.get("apparent_temperature_max") or [None])[0],
                "feels_low_c": (d.get("apparent_temperature_min") or [None])[0],
                "precip_prob_max_pct": (d.get("precipitation_probability_max") or [None])[0],
                "weather_code_daily": (d.get("weather_code") or [None])[0],
                "wind_max_kmh": (d.get("wind_speed_10m_max") or [None])[0],
                "uv_index_max": (d.get("uv_index_max") or [None])[0],
                "sunrise": (d.get("sunrise") or [None])[0],
                "sunset": (d.get("sunset") or [None])[0],
                "date": d["time"][0],
                "hourly": {
                    "time": h.get("time") or [],
                    "temp_c": h.get("temperature_2m") or [],
                    "feels_c": h.get("apparent_temperature") or [],
                    "precip_prob_pct": h.get("precipitation_probability") or [],
                    "weather_code": h.get("weather_code") or [],
                    "cloud_cover_pct": h.get("cloud_cover") or [],
                },
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
            winds: list[float] = []
            precips: list[float] = []
            clouds: list[float] = []
            symbols: list[str] = []
            hourly_time: list[str] = []
            hourly_temp: list[float] = []
            hourly_precip: list[float] = []
            hourly_cloud: list[float] = []
            for point in ts:
                try:
                    t = datetime.fromisoformat(point["time"].replace("Z", "+00:00"))
                except (KeyError, ValueError):
                    continue
                if t > cutoff:
                    break
                details = (point.get("data") or {}).get("instant", {}).get("details") or {}
                air_temp = details.get("air_temperature")
                if air_temp is None:
                    continue
                # Keep all hourly_* lists index-aligned with hourly_time. Missing
                # sub-fields land as 0.0 so _slice_window can compute medians
                # without IndexError; the daily aggregates filter Nones via the
                # separate non-hourly accumulator lists.
                temps.append(float(air_temp))
                hourly_temp.append(float(air_temp))
                hourly_time.append(point["time"])
                wind = details.get("wind_speed")
                if wind is not None:
                    winds.append(float(wind))
                cloud = details.get("cloud_area_fraction")
                if cloud is not None:
                    clouds.append(float(cloud))
                hourly_cloud.append(float(cloud) if cloud is not None else 0.0)
                n1 = (point.get("data") or {}).get("next_1_hours") or {}
                n6 = (point.get("data") or {}).get("next_6_hours") or {}
                n_block = n1 if n1 else n6
                sym = ((n_block.get("summary") or {}).get("symbol_code")) or ""
                if sym:
                    symbols.append(sym)
                precip_detail = (n_block.get("details") or {}).get("precipitation_amount")
                if precip_detail is not None:
                    precips.append(float(precip_detail))
                    hourly_precip.append(float(precip_detail))
                else:
                    hourly_precip.append(0.0)
            if not temps:
                return None
            precip_prob = None
            if precips:
                rainy_hours = sum(1 for p in precips if p > 0.1)
                precip_prob = round(100 * rainy_hours / len(precips))
            wind_max = max(winds) if winds else None
            return {
                "temp_high_c": max(temps),
                "temp_low_c": min(temps),
                "feels_high_c": None,
                "feels_low_c": None,
                "precip_prob_max_pct": precip_prob,
                "weather_code_daily": None,
                "wind_max_kmh": wind_max,
                "uv_index_max": None,
                "sunrise": None,
                "sunset": None,
                "symbol": symbols[0] if symbols else None,
                "hourly": {
                    "time": hourly_time,
                    "temp_c": hourly_temp,
                    # met.no exposes no apparent_temperature; consumers must
                    # treat absent feels as "fall back to temp_c".
                    "feels_c": list(hourly_temp),
                    # met.no exposes mm of liquid, not a probability. Treat
                    # any-nonzero as "100% rain this hour" so _slice_window
                    # sees a sensible binary signal rather than a unit-confused
                    # percentage.
                    "precip_prob_pct": [
                        100 if p > 0.1 else 0 for p in hourly_precip
                    ],
                    # met.no has no WMO codes; emit None so the consumer's
                    # disagreement merge can skip rather than averaging in 0.
                    "weather_code": [None] * len(hourly_time),
                    "cloud_cover_pct": hourly_cloud,
                },
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
            return {
                "temp_high_c": max(temps),
                "temp_low_c": min(temps),
                "feels_high_c": None,
                "feels_low_c": None,
                "precip_prob_max_pct": None,
                "weather_code_daily": None,
                "wind_max_kmh": None,
                "uv_index_max": None,
                "sunrise": None,
                "sunset": None,
            }
    except Exception:
        logger.exception("openweathermap fetch failed")
        return None


def _median(values: list) -> float | None:
    return round(statistics.median(values), 1) if values else None


def _consensus(sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not sources:
        return {"values": {}, "disagree": []}
    keys = [
        "temp_high_c", "temp_low_c", "feels_high_c", "feels_low_c",
        "precip_prob_max_pct", "wind_max_kmh", "uv_index_max",
    ]
    consensus: dict[str, Any] = {}
    disagree: list[str] = []
    for k in keys:
        named_vals = [(name, s[k]) for name, s in sources.items() if s.get(k) is not None]
        if not named_vals:
            continue
        vals = [v for _, v in named_vals]
        consensus[k] = _median(vals)
        if len(vals) > 1:
            spread = max(vals) - min(vals)
            threshold = 15 if "prob" in k else 5 if "wind" in k else 2
            if spread > threshold:
                # Include the source names so the per-source confidence
                # formula in fetch.py (`name in d`) actually downweights
                # participating sources instead of resolving False forever.
                names = ",".join(n for n, _ in named_vals)
                disagree.append(f"{k}: spread {spread:.1f} ({names})")
    return {"values": consensus, "disagree": disagree}
