"""``ytmusic_recent`` — most-recently-played tracks from YouTube Music.

YouTube Music has no true "now playing" API; this returns the recent
history feed, which is the closest public surface. ``ytmusicapi`` is
unofficial and ``get_history`` occasionally breaks when YouTube ships UI
changes, so the call is wrapped — on any failure Hikari gets a graceful
"yt music is being weird" message rather than a stack trace.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from claude_agent_sdk import tool

from agents import config as cfg
from tools._response import ok as _ok
from tools.ytmusic import _shared

logger = logging.getLogger(__name__)


@tool(
    "ytmusic_recent",
    "Get the most recently played tracks on YouTube Music. limit default 5. "
    "Note: no true 'now playing' API exists — this is recent history.",
    {"limit": int},
)
async def ytmusic_recent(args: dict[str, Any]) -> dict[str, Any]:
    limit = int(args.get("limit") or cfg.get("ytmusic.default_history_limit") or 5)
    loop = asyncio.get_event_loop()
    # ``_client`` is looked up on the ``_shared`` module so tests can
    # monkey-patch ``tools.ytmusic._shared._client`` and have the swap
    # observed here at call time.
    try:
        ytm = await loop.run_in_executor(None, lambda: _shared._client())
        history = await loop.run_in_executor(None, lambda: ytm.get_history()) or []
    except FileNotFoundError as e:
        return _ok(f"yt music isn't configured: {e}")
    except Exception as e:
        logger.exception("ytmusic_recent failed")
        return _ok(f"yt music is being weird right now ({type(e).__name__})")
    tracks = [_shared._shape_track(t) for t in history[:limit]]
    if not tracks:
        return _ok("no recent history", data={"tracks": []})
    lines = [f"recent ({len(tracks)}):"]
    for t in tracks:
        lines.append(f"  - {t['title']} — {', '.join(t['artists'])} ({t.get('played')})")
    return _ok("\n".join(lines), data={"tracks": tracks})
