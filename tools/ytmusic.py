"""Phase 10: YouTube Music read-only tools.

Auth: browser-cookie blob at YTMUSIC_BROWSER_JSON_PATH. See
scripts/setup_ytmusic.md for the one-time DevTools paste flow.

Caveat: ytmusicapi is unofficial. get_history occasionally breaks when
YouTube ships UI changes. All calls wrapped — on failure Hikari gets a
graceful 'yt music is being weird' message instead of a stack trace.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from claude_agent_sdk import tool

from agents import config as cfg
from tools._response import ok as _ok

logger = logging.getLogger(__name__)


def _client():
    """Build a YTMusic client from the cookie blob path. Raises on failure."""
    from ytmusicapi import YTMusic
    path_env = str(cfg.get("ytmusic.browser_json_path_env", "YTMUSIC_BROWSER_JSON_PATH"))
    path = os.environ.get(path_env)
    if not path:
        raise FileNotFoundError(
            f"{path_env} not set — see scripts/setup_ytmusic.md"
        )
    if not os.path.exists(path):
        raise FileNotFoundError(f"YT Music cookie blob not found at {path}")
    return YTMusic(path)


def _shape_track(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": t.get("title"),
        "artists": [a.get("name") for a in (t.get("artists") or []) if a.get("name")],
        "video_id": t.get("videoId"),
        "played": t.get("played"),
        "album": (t.get("album") or {}).get("name") if t.get("album") else None,
    }


@tool(
    "ytmusic_recent",
    "Get the most recently played tracks on YouTube Music. limit default 5. "
    "Note: no true 'now playing' API exists — this is recent history.",
    {"limit": int},
)
async def ytmusic_recent(args: dict[str, Any]) -> dict[str, Any]:
    limit = int(args.get("limit") or cfg.get("ytmusic.default_history_limit") or 5)
    loop = asyncio.get_event_loop()
    try:
        ytm = await loop.run_in_executor(None, lambda: _client())
        history = await loop.run_in_executor(None, lambda: ytm.get_history()) or []
    except FileNotFoundError as e:
        return _ok(f"yt music isn't configured: {e}")
    except Exception as e:
        logger.exception("ytmusic_recent failed")
        return _ok(f"yt music is being weird right now ({type(e).__name__})")
    tracks = [_shape_track(t) for t in history[:limit]]
    if not tracks:
        return _ok("no recent history", data={"tracks": []})
    lines = [f"recent ({len(tracks)}):"]
    for t in tracks:
        lines.append(f"  - {t['title']} — {', '.join(t['artists'])} ({t.get('played')})")
    return _ok("\n".join(lines), data={"tracks": tracks})


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
    try:
        ytm = await loop.run_in_executor(None, lambda: _client())
        results = await loop.run_in_executor(
            None, lambda: ytm.search(query, filter=filt, limit=limit)
        ) or []
    except FileNotFoundError as e:
        return _ok(f"yt music isn't configured: {e}")
    except Exception as e:
        logger.exception("ytmusic_search failed")
        return _ok(f"yt music is being weird right now ({type(e).__name__})")
    shaped = [_shape_track(r) for r in results[:limit]]
    return _ok(f"{len(shaped)} match(es) for {query!r}", data={"results": shaped})


@tool(
    "ytmusic_library",
    "List saved/library songs. limit default 25.",
    {"limit": int},
)
async def ytmusic_library(args: dict[str, Any]) -> dict[str, Any]:
    limit = int(args.get("limit") or 25)
    loop = asyncio.get_event_loop()
    try:
        ytm = await loop.run_in_executor(None, lambda: _client())
        songs = await loop.run_in_executor(
            None, lambda: ytm.get_library_songs(limit=limit)
        ) or []
    except FileNotFoundError as e:
        return _ok(f"yt music isn't configured: {e}")
    except Exception as e:
        logger.exception("ytmusic_library failed")
        return _ok(f"yt music is being weird right now ({type(e).__name__})")
    shaped = [_shape_track(s) for s in songs]
    return _ok(f"{len(shaped)} library tracks", data={"library": shaped})


ALL_TOOLS = [ytmusic_recent, ytmusic_search, ytmusic_library]
