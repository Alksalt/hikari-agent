"""Weather feature — manifest.

One tool (``weather_fetch``) backed by 2-3 free sources (open-meteo,
met.no, optional OpenWeatherMap). The per-source fetchers live in
``_sources.py``; the ``fetch_forecast`` library helper lives in
``_shared.py`` and is re-exported here because
``agents/morning_brief.py`` imports it directly (``from tools.weather
import fetch_forecast``).
"""
from __future__ import annotations

from tools.weather._shared import fetch_forecast  # noqa: F401 — external import target
from tools.weather.fetch import weather_fetch

ALL_TOOLS = [weather_fetch]
