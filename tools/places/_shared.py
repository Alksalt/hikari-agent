"""Shared helpers for the places tools.

Both ``places_search`` and ``place_open_now`` hit the OSM Overpass API
with the same query shape (``node["name"~…]``/``["amenity"=…]``/
``["shop"=…]`` around a lat/lon radius), so the QL builder, the
opening-hours parser shim, and the HTTP fetch live here. The per-tool
files stay thin.

Security: ``query`` is sanitized to ``[a-z0-9\\s\\-_'À-ɏ]`` before
interpolation. Overpass QL embeds the value into both regex-match
(``~"…"``) and literal-equality (``="…"``) string contexts; ``"``,
``\\``, ``]``, ``;``, and newline would all let an attacker escape the
quoted literal and inject arbitrary QL (SSRF via ``out``, huge bbox
dumps, etc.).

Heavy imports — ``httpx`` and ``osm_opening_hours_humanized`` — are
deferred to the call sites inside this module so importing the tool
manifest stays free.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from agents import config as cfg

logger = logging.getLogger(__name__)

# Cap on results we return to the model. Overpass can hand back hundreds
# for a popular amenity in a dense area; the model only needs a handful
# and pasting all of them blows the response budget.
_MAX_PLACES = 20

# Default radius if the caller omits ``radius_m`` and config has no
# override. 500m is a comfortable walk; wide enough to find a few
# options without flooding with results across a whole neighborhood.
_DEFAULT_RADIUS_M = 500

# Wider radius for the ``place_open_now`` single-name lookup. The user
# named a specific place; if it's not within 1km we probably have the
# wrong place anyway.
_OPEN_NOW_RADIUS_M = 1000

# Whitelist of characters allowed in a sanitized query. Latin letters,
# digits, whitespace, hyphen, underscore, apostrophe, and the extended
# Latin block (À-ɏ) so accented place names survive.
_QUERY_ALLOWED_RE = re.compile(r"[^a-z0-9\s\-_'À-ɏ]")

# Hard cap on query length post-sanitization. Overpass tolerates more
# but a 64-char place name is already generous and keeps the QL body
# bounded.
_QUERY_MAX_LEN = 64


def _open_now(opening_hours: str | None) -> bool | None:
    """Parse the OSM ``opening_hours`` tag and return open/closed/unknown.

    Returns ``True`` / ``False`` if the parser can decide, ``None`` if
    the tag is missing or the parser chokes (the OSM tag format is
    flexible enough that real-world data has corners the library
    doesn't handle — we'd rather say "unknown" than guess wrong).
    """
    if not opening_hours:
        return None
    try:
        # Lazy import — pulling pyparsing on every tool-module import
        # for a tag most places don't have is wasteful.
        from osm_opening_hours_humanized import OHParser  # noqa: PLC0415
        oh = OHParser(opening_hours)
        return oh.is_open()
    except Exception:
        return None


def _sanitize_query(raw: str) -> str:
    """Lowercase + strip dangerous chars from a user-supplied query.

    Returns the sanitized string (possibly empty if the input was all
    disallowed chars — callers should treat that as a refusal).
    """
    return _QUERY_ALLOWED_RE.sub("", raw.strip().lower())[:_QUERY_MAX_LEN]


def _build_overpass_query(query: str, lat: float, lon: float, radius: int) -> str:
    """Build the Overpass QL body for a name/amenity/shop search."""
    return f"""
[out:json][timeout:15];
(
  node["name"~"{query}", i](around:{radius},{lat},{lon});
  node["amenity"="{query}"](around:{radius},{lat},{lon});
  node["shop"="{query}"](around:{radius},{lat},{lon});
);
out tags center;
"""


async def _places_search_impl(args: dict[str, Any]) -> dict[str, Any]:
    """Core implementation shared by ``places_search`` and ``place_open_now``.

    Returns the same ``ok``-shaped response dict either tool exposes —
    the open-now wrapper just picks the top hit out of ``data.places``.
    Keeping this here (not on either tool file) avoids the two tools
    importing each other through the package ``__init__``.
    """
    # Local import — see module docstring; keeps the tool manifest cheap.
    import httpx  # noqa: PLC0415

    from tools._response import ok as _ok  # noqa: PLC0415

    raw_query = args.get("query") or ""
    lat = float(args.get("lat") or 0)
    lon = float(args.get("lon") or 0)
    radius = int(
        args.get("radius_m") or cfg.get("places.default_radius_m") or _DEFAULT_RADIUS_M
    )
    if not raw_query.strip():
        return _ok("refused: empty query")
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return _ok("refused: lat/lon out of range")
    query = _sanitize_query(raw_query)
    if not query:
        return _ok("refused: query had no usable characters after sanitization")

    overpass_q = _build_overpass_query(query, lat, lon, radius)
    endpoint = str(cfg.get(
        "places.overpass_endpoint",
        "https://overpass-api.de/api/interpreter",
    ))
    ua = str(cfg.get("places.user_agent", "hikari-agent/0.1"))
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                endpoint,
                data={"data": overpass_q},
                headers={"User-Agent": ua},
            )
            r.raise_for_status()
            elements = (r.json() or {}).get("elements") or []
    except Exception as e:
        logger.exception("overpass query failed")
        return _ok(f"overpass error: {e}", data={"error": str(e), "places": []})

    places: list[dict[str, Any]] = []
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
        if len(places) >= _MAX_PLACES:
            break

    if not places:
        return _ok(f"no results for {query!r} within {radius}m", data={"places": []})
    lines = [f"found {len(places)}:"]
    for p in places[:10]:
        status = (
            "open" if p["open_now"] is True
            else "closed" if p["open_now"] is False
            else "hours unknown"
        )
        lines.append(f"  - {p['name']} ({p['amenity'] or '?'}) — {status}")
    return _ok("\n".join(lines), data={"places": places})
