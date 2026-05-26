"""``generate_photo`` — queue a Hikari photo for the Telegram bridge."""
from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)

# Shared runtime_state flag — the bridge polls this after each LLM turn and,
# if it's set within the last 60s, force-sends a sticker so the user gets
# *something* visual instead of an empty "image gen down" abdication.
IMAGE_GEN_FAILURE_KEY = "image_gen_last_failure_ts"


@tool(
    "generate_photo",
    "Generate a photo of Hikari (selfie or candid) and drop it in the outbox so the "
    "Telegram bridge will send it with your next text reply. Mood-gated: unprompted "
    "sends require mood='weirdly good'; user-requested sends bypass the mood gate. "
    "Daily-capped. Pass mood='' to read from core_blocks. "
    "Pass unprompted=true when the call originates from a background/proactive path "
    "(not from a user request). "
    "Returns 'queued' on success or 'refused: <reason>'.",
    {"mood": str, "unprompted": bool},
    annotations=annotations_for("generate_photo"),
)
async def generate_photo(args: dict[str, Any]) -> dict[str, Any]:
    mood = _resolve_mood(str(args.get("mood") or ""))
    unprompted = bool(args.get("unprompted", False))

    # Mood gate — irritable always blocks; unprompted requires 'weirdly good'.
    if mood == "irritable":
        return {"content": [{"type": "text", "text": "refused: mood is irritable"}]}
    if unprompted and mood != "weirdly good":
        return {"content": [{"type": "text", "text": f"refused: unprompted photo requires weirdly good mood (current: {mood})"}]}

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
    try:
        ikey = f"photo_generated_{fname}"
        db.media_outbox_insert(
            "photo",
            ikey,
            {"path": str(path), "caption": "", "chat_id": None},
        )
    except Exception:
        logger.exception("generate_photo: media_outbox_insert failed; not bumping counter, removing orphan %s", fname)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return {"content": [{"type": "text", "text": (
            "image_gen_down: outbox write failed. the bridge will send a sticker. "
            "say nothing about image generation in your reply."
        )}]}
    _record_photo_sent()
    return {"content": [{"type": "text", "text": f"queued {path.name} ({len(img_bytes)} bytes)"}]}
