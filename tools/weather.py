"""Phase 10: multi-source weather forecast.

Sources: open-meteo (no key) + met.no/yr (no key, attribution via User-Agent).
Optional 3rd: OpenWeatherMap via OPENWEATHERMAP_API_KEY.

The morning_brief consumer ingests the merged result and feeds it to
run_proactive — Hikari rewrites in voice.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from claude_agent_sdk import tool

from agents import config as cfg

logger = logging.getLogger(__name__)


def _ok(text: str, data: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if data is not None:
        body["data"] = data
    return body


async def _fetch_open_meteo(lat: float, lon: float) -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.open-meteo.com/v1/forecast",
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
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.met.no/weatherapi/locationforecast/2.0/compact",
                params={"lat": lat, "lon": lon},
                headers={"User-Agent": "hikari-agent/0.1 (github.com/Alksalt/hikari-agent)"},
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
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.openweathermap.org/data/2.5/forecast",
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


async def fetch_forecast(lat: float, lon: float) -> dict[str, Any]:
    """Merge configured sources. Returns
    {sources: {name: {...}}, consensus: {...}, lat, lon}."""
    enabled = cfg.get("morning_brief.sources") or ["open_meteo", "met_no"]
    out: dict[str, dict[str, Any]] = {}
    if "open_meteo" in enabled:
        r = await _fetch_open_meteo(lat, lon)
        if r: out["open_meteo"] = r
    if "met_no" in enabled:
        r = await _fetch_met_no(lat, lon)
        if r: out["met_no"] = r
    if "openweathermap" in enabled:
        r = await _fetch_openweathermap(lat, lon)
        if r: out["openweathermap"] = r
    return {"sources": out, "consensus": _consensus(out), "lat": lat, "lon": lon}


@tool(
    "weather_fetch",
    "Fetch today's weather forecast at (lat, lon) merged from 2-3 free sources "
    "(open-meteo, met.no, optional openweathermap). Returns high, low, and per-source "
    "raw payloads. Used by the daily morning_brief job; Hikari may also call it "
    "directly when the user asks for weather at a different location.",
    {"lat": float, "lon": float},
)
async def weather_fetch(args: dict[str, Any]) -> dict[str, Any]:
    lat = float(args.get("lat") or 0)
    lon = float(args.get("lon") or 0)
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return _ok("refused: lat/lon out of range")
    out = await fetch_forecast(lat, lon)
    if not out["sources"]:
        return _ok("all weather sources failed", data=out)
    c = out["consensus"]
    summary = (
        f"forecast for ({lat:.2f},{lon:.2f}): "
        f"high {c.get('temp_high_c')}°C, low {c.get('temp_low_c')}°C "
        f"(sources: {', '.join(out['sources'].keys())})"
    )
    return _ok(summary, data=out)


ALL_TOOLS = [weather_fetch]
