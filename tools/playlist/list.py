"""``playlist_list`` — return Hikari's curated track list from config/hikari_playlist.yaml.

No network calls. Reads only the local YAML. Optional filters: mood_tag, topic
substring match against voice_annotation, and limit.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from claude_agent_sdk import tool

from tools._annotations import annotations_for
from tools._response import ok as _ok

logger = logging.getLogger(__name__)

_PLAYLIST_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "hikari_playlist.yaml"


def _load_tracks() -> list[dict[str, Any]]:
    try:
        raw = yaml.safe_load(_PLAYLIST_PATH.read_text(encoding="utf-8"))
        return raw.get("tracks") or []
    except Exception:
        logger.exception("playlist_list: failed to read %s", _PLAYLIST_PATH)
        return []


@tool(
    "playlist_list",
    (
        "Return Hikari's curated track list from config/hikari_playlist.yaml. "
        "Each track has id, title, artist, mood_tag, and voice_annotation. "
        "Use when the music topic comes up — surface the actual curated list "
        "instead of hallucinating titles. "
        "mood: filter by exact mood_tag (e.g. 'late_night', 'working', "
        "'winter_dawn', 'focused', 'irritable', 'autumn'). "
        "topic: substring match against voice_annotation text (case-insensitive). "
        "limit: max tracks to return (default 5, max 20)."
    ),
    {"mood": str, "topic": str, "limit": int},
    annotations=annotations_for("playlist_list"),
)
async def playlist_list(args: dict[str, Any]) -> dict[str, Any]:
    mood = (args.get("mood") or "").strip().lower()
    topic = (args.get("topic") or "").strip().lower()
    limit = min(int(args.get("limit") or 5), 20)

    tracks = _load_tracks()
    if not tracks:
        return _ok("playlist file is missing or empty")

    # Apply filters
    if mood:
        tracks = [t for t in tracks if (t.get("mood_tag") or "").lower() == mood]
    if topic:
        tracks = [t for t in tracks if topic in (t.get("voice_annotation") or "").lower()]

    tracks = tracks[:limit]

    if not tracks:
        filter_desc = " / ".join(f for f in [mood, topic] if f) or "none"
        return _ok(f"no tracks match filter ({filter_desc})")

    # Build a summary line for the text envelope
    mood_str = f" [{mood}]" if mood else ""
    summary = f"{len(tracks)} track(s){mood_str}"

    return _ok(
        summary,
        data={"tracks": tracks, "count": len(tracks)},
        presentation_hint="list_of_records",
    )
