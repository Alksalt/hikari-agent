"""``generate_photo`` — queue a Hikari photo for the Telegram bridge."""
from __future__ import annotations

import time
from typing import Any

from claude_agent_sdk import tool

from tools.photos._shared import (
    DAILY_CAP,
    DEFAULT_MODEL,
    OUTBOX,
    _call_flux,
    _photos_sent_today,
    _read_appearance_base,
    _record_photo_sent,
    _resolve_mood,
    _scene_suffix,
)


@tool(
    "generate_photo",
    "Generate a photo of Hikari (selfie or candid) and drop it in the outbox so the "
    "Telegram bridge will send it with your next text reply. Mood-gated (refuses if "
    "irritable), daily-capped. Pass mood='' to read from core_blocks. Returns 'queued' "
    "on success or 'refused: <reason>'.",
    {"mood": str},
)
async def generate_photo(args: dict[str, Any]) -> dict[str, Any]:
    mood = _resolve_mood(str(args.get("mood") or ""))
    if mood == "irritable":
        return {"content": [{"type": "text", "text": "refused: mood is irritable"}]}
    if _photos_sent_today() >= DAILY_CAP:
        return {"content": [{"type": "text", "text": f"refused: daily cap reached ({DAILY_CAP})"}]}

    base = _read_appearance_base()
    suffix = _scene_suffix(mood)
    prompt = f"{base}, {suffix}".strip(", ")
    img_bytes = await _call_flux(prompt, DEFAULT_MODEL)
    if not img_bytes:
        return {"content": [{"type": "text", "text": "refused: image generation failed"}]}

    OUTBOX.mkdir(parents=True, exist_ok=True)
    fname = f"{int(time.time() * 1000)}.png"
    path = OUTBOX / fname
    path.write_bytes(img_bytes)
    _record_photo_sent()
    return {"content": [{"type": "text", "text": f"queued {path.name} ({len(img_bytes)} bytes)"}]}
