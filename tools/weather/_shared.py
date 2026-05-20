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


async def fetch_forecast(lat: float, lon: float) -> dict[str, Any]:
    """Merge configured sources. Returns
    ``{sources: {name: {...}}, consensus: {...}, lat, lon}``.
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
    return {"sources": out, "consensus": _consensus(out), "lat": lat, "lon": lon}
