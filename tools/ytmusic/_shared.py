"""Shared helpers for the YouTube Music tools.

The ``ytmusicapi`` package is heavy (drags ``requests`` + a pile of
catalog-shape modules) and only useful when the cookie blob is
configured, so we import it lazily inside ``_client`` rather than at
module top. That keeps utility-server startup fast on machines without
YT Music wired up.

Auth: a browser-cookie blob (DevTools paste flow — see
``scripts/setup_ytmusic.md``) whose path lives in the env var named by
``ytmusic.browser_json_path_env`` (default ``YTMUSIC_BROWSER_JSON_PATH``).

Track-shape normalizer ``_shape_track`` is shared across all three
tools so the response schema stays consistent regardless of which
``ytmusicapi`` call produced the row.
"""
from __future__ import annotations

import os
from typing import Any

from agents import config as cfg


def _client():
    """Build a YTMusic client from the cookie blob path. Raises on failure.

    ``ytmusicapi`` is imported here, not at module top, so the registry
    sweep doesn't pay the import cost on every process startup. Callers
    must be ready for ``FileNotFoundError`` when the cookie blob env var
    is unset or the file is missing — handlers translate that into a
    graceful "yt music isn't configured" message.
    """
    from ytmusicapi import YTMusic  # noqa: PLC0415 — lazy load
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
    """Normalize a ``ytmusicapi`` track dict into our response shape.

    Different endpoints (``get_history`` / ``search`` / ``get_library_songs``)
    return slightly different shapes; this collapses them into the
    common subset we care about (title, artists, video id, played-at,
    album). Missing fields become ``None`` / empty lists.
    """
    return {
        "title": t.get("title"),
        "artists": [a.get("name") for a in (t.get("artists") or []) if a.get("name")],
        "video_id": t.get("videoId"),
        "played": t.get("played"),
        "album": (t.get("album") or {}).get("name") if t.get("album") else None,
    }
