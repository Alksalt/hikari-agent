"""``weather_fetch`` — today's forecast at (lat, lon), merged across sources.

Thin wrapper over ``fetch_forecast`` in ``_shared``: validates the
coordinates, runs the merge, and formats a one-line summary so Hikari
can rewrite in voice (the per-source payload still rides along under
``data`` for downstream consumers like ``morning_brief``).
"""
from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from tools._response import ok as _ok
from tools.weather._shared import fetch_forecast


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
