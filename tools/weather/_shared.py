"""Shared bits for the weather feature.

Endpoint URLs + the ``fetch_forecast`` library helper. The helper is
re-exported from the package so callers (``agents/morning_brief.py``)
can import it without depending on the tool-handler module.

The per-source fetchers live in ``_sources.py``. We import them inside
``fetch_forecast`` rather than at module top to avoid a cycle:
``_sources`` reads the URL constants from this module, so importing
``_sources`` at the top of ``_shared`` would force-load it before its
own constants finished defining. Function-local import sidesteps that
and costs nothing — ``_sources`` is already imported by the time
anyone calls ``fetch_forecast``.
"""
from __future__ import annotations

import statistics
from datetime import datetime
from typing import Any

from agents import config as cfg

# Endpoint URLs — kept as constants so per-source fetchers and any
# future tests can reference them by name. Order matches the source
# priority used in ``fetch_forecast``.
_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
_MET_NO_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
_OPENWEATHERMAP_URL = "https://api.openweathermap.org/data/2.5/forecast"

# met.no requires a User-Agent for attribution; bake the project
# identity in once here so the per-source fetcher just references it.
_MET_NO_USER_AGENT = "hikari-agent/0.1 (github.com/Alksalt/hikari-agent)"

_WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "rime fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    71: "light snow", 73: "snow", 75: "heavy snow",
    80: "showers", 81: "showers", 82: "violent showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "severe thunderstorm",
}


def wmo_label(code: int | None) -> str:
    if code is None:
        return "code None"
    return _WMO.get(code, f"code {code}")


def _slice_window(hourly: dict, start_hour: int, end_hour: int) -> dict[str, Any]:
    """Median temp/feels/precip/wcode/cloud across [start_hour, end_hour) local time."""
    rows = []
    for i, t in enumerate(hourly.get("time", []) or []):
        try:
            hr = datetime.fromisoformat(t).hour
        except (ValueError, TypeError):
            continue
        if start_hour <= hr < end_hour:
            rows.append({
                "temp": hourly["temp_c"][i] if i < len(hourly.get("temp_c") or []) else None,
                "feels": hourly["feels_c"][i] if i < len(hourly.get("feels_c") or []) else None,
                "precip": (
                    hourly["precip_prob_pct"][i]
                    if i < len(hourly.get("precip_prob_pct") or []) else None
                ),
                "wcode": (
                    hourly["weather_code"][i]
                    if i < len(hourly.get("weather_code") or []) else None
                ),
                "cloud": (
                    hourly["cloud_cover_pct"][i]
                    if i < len(hourly.get("cloud_cover_pct") or []) else None
                ),
            })
    rows = [r for r in rows if all(v is not None for v in r.values())]
    if not rows:
        return {}
    most_common_wcode = max(
        set(r["wcode"] for r in rows),
        key=lambda c: sum(1 for r in rows if r["wcode"] == c),
    )
    return {
        "temp_c": round(statistics.median(r["temp"] for r in rows), 1),
        "feels_c": round(statistics.median(r["feels"] for r in rows), 1),
        "precip_prob_pct": round(statistics.median(r["precip"] for r in rows)),
        "weather_code": most_common_wcode,
        "cloud_cover_pct": round(statistics.median(r["cloud"] for r in rows)),
    }


async def fetch_forecast(lat: float, lon: float) -> dict[str, Any]:
    """Merge configured sources. Returns
    ``{sources: {name: {...}}, consensus: {values, disagree}, lat, lon}``.
    """
    # Function-local import — see module docstring for the cycle reason.
    from tools.weather._sources import (
        _consensus,
        _fetch_met_no,
        _fetch_open_meteo,
        _fetch_openweathermap,
    )

    enabled = cfg.get("morning_brief.sources") or ["open_meteo", "met_no"]
    out: dict[str, dict[str, Any]] = {}
    if "open_meteo" in enabled:
        r = await _fetch_open_meteo(lat, lon)
        if r:
            out["open_meteo"] = r
    if "met_no" in enabled:
        r = await _fetch_met_no(lat, lon)
        if r:
            out["met_no"] = r
    if "openweathermap" in enabled:
        r = await _fetch_openweathermap(lat, lon)
        if r:
            out["openweathermap"] = r
    # Window-slice + sunrise/sunset against the richest source so every
    # caller (the weather_fetch MCP tool AND morning_brief, which calls
    # fetch_forecast directly) sees them — not just the tool wrapper.
    primary = out.get("open_meteo") or (next(iter(out.values())) if out else {})
    hourly = primary.get("hourly", {}) if primary else {}
    windows = {
        "morning": _slice_window(hourly, 7, 10),
        "midday":  _slice_window(hourly, 12, 15),
        "evening": _slice_window(hourly, 18, 21),
    } if hourly else {}
    return {
        "sources": out,
        "consensus": _consensus(out),
        "windows": windows,
        "sunrise": primary.get("sunrise") if primary else None,
        "sunset": primary.get("sunset") if primary else None,
        "lat": lat,
        "lon": lon,
    }
