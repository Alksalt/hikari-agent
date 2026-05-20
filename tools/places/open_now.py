"""``place_open_now`` — is one named place open right now?"""
from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from tools._response import ok as _ok
from tools.places._shared import _OPEN_NOW_RADIUS_M, _places_search_impl


@tool(
    "place_open_now",
    "Check whether ONE specific named place is open right now. Searches a wider "
    "1km radius around lat/lon and returns the first match's open/closed/unknown state. "
    "e.g. user asks 'is Blue Bottle on 5th open' → place_open_now('Blue Bottle', …). "
    "Don't use this to browse options (use `places_search` with an amenity type).",
    {"name": str, "lat": float, "lon": float},
)
async def place_open_now(args: dict[str, Any]) -> dict[str, Any]:
    name = (args.get("name") or "").strip()
    if not name:
        return _ok("refused: empty name")
    search_out = await _places_search_impl({
        "query": name,
        "lat": args.get("lat"),
        "lon": args.get("lon"),
        "radius_m": _OPEN_NOW_RADIUS_M,
    })
    places = search_out.get("data", {}).get("places") or []
    if not places:
        return _ok(f"no place named {name!r} found nearby", data={"open_now": None})
    p = places[0]
    return _ok(
        f"{p['name']}: " + (
            "open" if p["open_now"] is True
            else "closed" if p["open_now"] is False
            else "OSM has no opening hours tagged for this place — i can't tell"
        ),
        data={"place": p, "open_now": p["open_now"]},
    )
