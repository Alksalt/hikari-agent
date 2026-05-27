"""``weather_fetch`` — today's forecast at (lat, lon), merged across sources.

Thin wrapper over ``fetch_forecast`` in ``_shared``: validates the
coordinates, runs the merge, and formats a three-window summary so
Hikari can render morning / midday / evening at a glance.

Location fallback chain (applied when lat/lon args are absent or zero):
  1. Explicit lat/lon args from the caller
  2. HOME_LAT / HOME_LON environment variables
  3. config weather.default_location (Kristiansund by default)
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from claude_agent_sdk import tool

from agents import config as cfg
from tools._annotations import annotations_for
from tools._response import ok as _ok
from tools.weather._shared import fetch_forecast, wmo_label


def _resolve_coords(args: dict[str, Any]) -> tuple[float, float, str]:
    """Resolve (lat, lon, label) using the documented fallback chain."""
    raw_lat = args.get("lat")
    raw_lon = args.get("lon")
    label = (args.get("label") or "").strip()

    # 1. Explicit args — only accept if both are non-zero and valid.
    if raw_lat and raw_lon:
        try:
            lat, lon = float(raw_lat), float(raw_lon)
            if (-90 <= lat <= 90) and (-180 <= lon <= 180) and (lat != 0.0 or lon != 0.0):
                return lat, lon, label
        except (TypeError, ValueError):
            pass

    # 2. HOME_LAT / HOME_LON env vars.
    lat_env = os.environ.get("HOME_LAT")
    lon_env = os.environ.get("HOME_LON")
    if lat_env and lon_env:
        try:
            lat, lon = float(lat_env), float(lon_env)
            if (-90 <= lat <= 90) and (-180 <= lon <= 180):
                return lat, lon, label or "home"
        except ValueError:
            pass

    # 3. Config default location.
    default = cfg.get("weather.default_location") or {}
    if isinstance(default, dict) and default.get("lat") and default.get("lon"):
        try:
            lat, lon = float(default["lat"]), float(default["lon"])
            city = str(default.get("city") or "")
            return lat, lon, label or city
        except (TypeError, ValueError):
            pass

    # Fallback to whatever the caller gave (may be zero — will fail validation below).
    return float(raw_lat or 0), float(raw_lon or 0), label


@tool(
    "weather_fetch",
    "Today's forecast at (lat, lon, optional label). Returns three time "
    "windows (morning 07-10, midday 12-15, evening 18-21) plus daily "
    "high/low, feels-like, max rain probability, UV, wind, sunrise/sunset. "
    "Merged across 2 free sources (open-meteo + met.no) with per-field "
    "median consensus + disagreement flags.",
    {"lat": float, "lon": float, "label": str},
    annotations=annotations_for("weather_fetch"),
)
async def weather_fetch(args: dict[str, Any]) -> dict[str, Any]:
    lat, lon, label = _resolve_coords(args)
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return _ok("refused: lat/lon out of range")
    out = await fetch_forecast(lat, lon)
    if not out["sources"]:
        return _ok(
            "all weather sources failed",
            sources=[],
            presentation_hint="weather_three_window",
        )

    windows = out.get("windows") or {}
    c = out["consensus"]["values"]
    disagree = out["consensus"]["disagree"]
    location_label = label or f"({lat:.2f},{lon:.2f})"
    rain_max = c.get("precip_prob_max_pct")
    summary = (
        f"forecast for {location_label}: "
        f"high {c.get('temp_high_c')}°C / low {c.get('temp_low_c')}°C "
        f"(feels {c.get('feels_low_c')}–{c.get('feels_high_c')}°C). "
        f"max rain prob {rain_max}%. "
        f"morning {(windows.get('morning') or {}).get('temp_c')}°C "
        f"{wmo_label((windows.get('morning') or {}).get('weather_code'))}, "
        f"midday {(windows.get('midday') or {}).get('temp_c')}°C "
        f"{wmo_label((windows.get('midday') or {}).get('weather_code'))}, "
        f"evening {(windows.get('evening') or {}).get('temp_c')}°C "
        f"{wmo_label((windows.get('evening') or {}).get('weather_code'))}."
    )
    return _ok(
        summary,
        data={
            "location": {"label": location_label, "lat": lat, "lon": lon},
            "consensus": c,
            "windows": windows,
            "sunrise": out.get("sunrise"),
            "sunset": out.get("sunset"),
            "uv_index_max": c.get("uv_index_max"),
            "wind_max_kmh": c.get("wind_max_kmh"),
            "per_source": {k: {kk: vv for kk, vv in v.items() if kk != "hourly"}
                           for k, v in out["sources"].items()},
        },
        sources=[
            {"name": name, "url": None,
             "fetched_at": datetime.now(UTC).isoformat(),
             "confidence": 1.0 / (1 + sum(1 for d in disagree if name in d))}
            for name in out["sources"].keys()
        ],
        presentation_hint="weather_three_window",
        notes=disagree or None,
    )
