"""Playlist feature — Hikari's curated track list.

Exposes ``playlist_list``: reads ``config/hikari_playlist.yaml`` and returns
the annotated track list with optional mood/topic/limit filters. No network
calls — local YAML only.

Auto-discovered by ``tools._registry`` and merged into the ``hikari_utility``
MCP server.
"""
from __future__ import annotations

from tools.playlist.list import playlist_list

ALL_TOOLS = [playlist_list]
