"""``ytmusic_search`` — search the YouTube Music catalog.

Wraps ``ytmusicapi.YTMusic.search``. As with the other ytmusic tools
the call is wrapped so an unofficial-API breakage degrades to a
graceful message instead of a stack trace.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from claude_agent_sdk import tool

from tools._response import ok as _ok
from tools.ytmusic import _shared

logger = logging.getLogger(__name__)


@tool(
    "ytmusic_search",
    "Search the YouTube Music catalog. filter one of {songs, albums, artists, "
    "playlists, videos}; default 'songs'. limit default 10.",
    {"query": str, "filter": str, "limit": int},
)
async def ytmusic_search(args: dict[str, Any]) -> dict[str, Any]:
    query = (args.get("query") or "").strip()
    if not query:
        return _ok("refused: empty query")
    filt = (args.get("filter") or "songs").strip()
    limit = int(args.get("limit") or 10)
    loop = asyncio.get_event_loop()
    # ``_client`` is looked up on the ``_shared`` module so tests can
    # monkey-patch ``tools.ytmusic._shared._client`` and have the swap
    # observed here at call time.
    try:
        ytm = await loop.run_in_executor(None, lambda: _shared._client())
        results = await loop.run_in_executor(
            None, lambda: ytm.search(query, filter=filt, limit=limit)
        ) or []
    except FileNotFoundError as e:
        return _ok(f"yt music isn't configured: {e}")
    except Exception as e:
        logger.exception("ytmusic_search failed")
        return _ok(f"yt music is being weird right now ({type(e).__name__})")
    shaped = [_shared._shape_track(r) for r in results[:limit]]
    return _ok(f"{len(shaped)} match(es) for {query!r}", data={"results": shaped})
