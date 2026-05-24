"""``generate_photo`` — queue a Hikari photo for the Telegram bridge."""
from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from claude_agent_sdk import tool

from storage import db
from tools._annotations import annotations_for
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

# Shared runtime_state flag — the bridge polls this after each LLM turn and,
# if it's set within the last 60s, force-sends a sticker so the user gets
# *something* visual instead of an empty "image gen down" abdication.
IMAGE_GEN_FAILURE_KEY = "image_gen_last_failure_ts"


@tool(
    "generate_photo",
    "Generate a photo of Hikari (selfie or candid) and drop it in the outbox so the "
    "Telegram bridge will send it with your next text reply. Mood-gated (refuses if "
    "irritable), daily-capped. Pass mood='' to read from core_blocks. Returns 'queued' "
    "on success or 'refused: <reason>'.",
    {"mood": str},
    annotations=annotations_for("generate_photo"),
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
        # Set a runtime flag so the bridge's _send_with_choreography can
        # detect this failure AFTER the LLM reply ships and force-send a
        # sticker as a fallback. We do this via shared state (not the
        # returned text) so the bridge doesn't depend on the LLM echoing
        # any particular token — the LLM is explicitly instructed NOT to
        # mention image generation in its reply.
        db.runtime_set(IMAGE_GEN_FAILURE_KEY, datetime.now(UTC).isoformat())
        return {"content": [{"type": "text", "text": (
            "image_gen_down: tool failed. the bridge will send a sticker. "
            "say nothing about image generation in your reply."
        )}]}

    OUTBOX.mkdir(parents=True, exist_ok=True)
    fname = f"{int(time.time() * 1000)}.png"
    path = OUTBOX / fname
    path.write_bytes(img_bytes)
    _record_photo_sent()
    # Insert a media_outbox row so the drainer uses the DB queue, not filesystem scan.
    try:
        ikey = f"photo_generated_{fname}"
        db.media_outbox_insert(
            "photo",
            ikey,
            {"path": str(path), "caption": "", "chat_id": None},
        )
    except Exception:
        pass  # non-fatal: bridge will reconcile orphan on next boot
    return {"content": [{"type": "text", "text": f"queued {path.name} ({len(img_bytes)} bytes)"}]}
