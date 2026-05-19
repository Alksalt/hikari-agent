"""Phase 10: places search via OSM Overpass + opening_hours parser.

Overpass: free, no key, ~10k req/day fair-use. opening_hours tag is parsed
via osm-opening-hours-humanized to compute "open now". Coverage is patchy
outside dense European cities — tell Hikari when hours are missing rather
than guessing.
"""
from __future__ import annotations

import logging
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


def _open_now(opening_hours: str | None) -> bool | None:
    if not opening_hours:
        return None
    try:
        from humanized_opening_hours import OHParser
        oh = OHParser(opening_hours)
        return oh.is_open()
    except Exception:
        return None


@tool(
    "places_search",
    "Search nearby POIs via OSM Overpass. query is a free-form name or amenity "
    "type (e.g. 'cafe', 'bakery', 'pharmacy'). radius_m default 500. Returns "
    "name, address-ish, opening_hours raw, open_now (true/false/null). null means "
    "OSM has no hours tagged — tell the user honestly.",
    {"query": str, "lat": float, "lon": float, "radius_m": int},
)
async def places_search(args: dict[str, Any]) -> dict[str, Any]:
    query = (args.get("query") or "").strip().lower()
    lat = float(args.get("lat") or 0)
    lon = float(args.get("lon") or 0)
    radius = int(args.get("radius_m") or cfg.get("places.default_radius_m") or 500)
    if not query:
        return _ok("refused: empty query")
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return _ok("refused: lat/lon out of range")
    # Sanitize query before Overpass QL interpolation. The query goes into both
    # regex (`~"…"`) and literal (`="…"`) string contexts; ", \, ], ;, \n would
    # all let an attacker escape the literal and inject arbitrary QL (SSRF,
    # large-bbox dumps). Allow only the chars a real place-search query needs.
    import re as _re
    query = _re.sub(r"[^a-z0-9\s\-_'À-ɏ]", "", query)[:64]
    if not query:
        return _ok("refused: query had no usable characters after sanitization")

    overpass_q = f"""
[out:json][timeout:15];
(
  node["name"~"{query}", i](around:{radius},{lat},{lon});
  node["amenity"="{query}"](around:{radius},{lat},{lon});
  node["shop"="{query}"](around:{radius},{lat},{lon});
);
out tags center;
"""
    endpoint = str(cfg.get(
        "places.overpass_endpoint",
        "https://overpass-api.de/api/interpreter",
    ))
    ua = str(cfg.get("places.user_agent", "hikari-agent/0.1"))
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(endpoint, data={"data": overpass_q},
                                  headers={"User-Agent": ua})
            r.raise_for_status()
            elements = (r.json() or {}).get("elements") or []
    except Exception as e:
        logger.exception("overpass query failed")
        return _ok(f"overpass error: {e}", data={"error": str(e), "places": []})

    places = []
    for el in elements:
        tags = el.get("tags") or {}
        name = tags.get("name") or "(unnamed)"
        hours = tags.get("opening_hours")
        places.append({
            "name": name,
            "amenity": tags.get("amenity") or tags.get("shop"),
            "hours": hours,
            "open_now": _open_now(hours),
            "lat": el.get("lat"),
            "lon": el.get("lon"),
            "osm_id": el.get("id"),
        })
        if len(places) >= 20:
            break

    if not places:
        return _ok(f"no results for {query!r} within {radius}m", data={"places": []})
    lines = [f"found {len(places)}:"]
    for p in places[:10]:
        status = "open" if p["open_now"] is True else \
                 "closed" if p["open_now"] is False else "hours unknown"
        lines.append(f"  - {p['name']} ({p['amenity'] or '?'}) — {status}")
    return _ok("\n".join(lines), data={"places": places})


@tool(
    "place_open_now",
    "Convenience: check whether a specific place by name is open right now. "
    "Wraps places_search and returns the first match.",
    {"name": str, "lat": float, "lon": float},
)
async def place_open_now(args: dict[str, Any]) -> dict[str, Any]:
    name = (args.get("name") or "").strip()
    if not name:
        return _ok("refused: empty name")
    search_out = await places_search.handler({
        "query": name, "lat": args.get("lat"), "lon": args.get("lon"),
        "radius_m": 1000,
    })
    places = search_out.get("data", {}).get("places") or []
    if not places:
        return _ok(f"no place named {name!r} found nearby", data={"open_now": None})
    p = places[0]
    return _ok(
        f"{p['name']}: " + (
            "open" if p["open_now"] is True else
            "closed" if p["open_now"] is False else
            "OSM has no opening hours tagged for this place — i can't tell"
        ),
        data={"place": p, "open_now": p["open_now"]},
    )


ALL_TOOLS = [places_search, place_open_now]
