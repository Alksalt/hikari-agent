"""``ytmusic_library`` — list saved/library songs from YouTube Music.

Wraps ``ytmusicapi.YTMusic.get_library_songs``. As with the other
ytmusic tools the call is wrapped so an unofficial-API breakage
degrades to a graceful message instead of a stack trace.
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
    "ytmusic_library",
    "List saved/library songs. limit default 25.",
    {"limit": int},
)
async def ytmusic_library(args: dict[str, Any]) -> dict[str, Any]:
    limit = int(args.get("limit") or 25)
    loop = asyncio.get_event_loop()
    # ``_client`` is looked up on the ``_shared`` module so tests can
    # monkey-patch ``tools.ytmusic._shared._client`` and have the swap
    # observed here at call time.
    try:
        ytm = await loop.run_in_executor(None, lambda: _shared._client())
        songs = await loop.run_in_executor(
            None, lambda: ytm.get_library_songs(limit=limit)
        ) or []
    except FileNotFoundError as e:
        return _ok(f"yt music isn't configured: {e}")
    except Exception as e:
        logger.exception("ytmusic_library failed")
        return _ok(f"yt music is being weird right now ({type(e).__name__})")
    shaped = [_shared._shape_track(s) for s in songs]
    return _ok(f"{len(shaped)} library tracks", data={"library": shaped})
