"""``places_search`` — nearby POI search via OSM Overpass."""
from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from tools.places._shared import _places_search_impl


@tool(
    "places_search",
    "Search nearby physical places / POIs by amenity type or name via OSM Overpass. "
    "Requires lat/lon (typically from the user's shared location). radius_m default 500. "
    "Returns name, type, opening_hours raw, and open_now (true/false/null where null "
    "means OSM has no hours tagged — say so honestly). "
    "e.g. user asks 'is there a bakery near me open right now' → places_search('bakery', …). "
    "Don't use this when you already know the specific place name (use `place_open_now` "
    "for a focused 'is X open?' check).",
    {"query": str, "lat": float, "lon": float, "radius_m": int},
)
async def places_search(args: dict[str, Any]) -> dict[str, Any]:
    return await _places_search_impl(args)
