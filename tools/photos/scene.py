"""Scene photo generation — environment/object photos with no face/character.

Used 1-2x/week, mood-gated, daily-capped (shares pool with selfie gen).
Prompts built from hikari_current_activity + time_texture + season.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from claude_agent_sdk import tool

from agents import config as cfg
from storage import db
from tools._annotations import annotations_for
from tools.photos._shared import DEFAULT_MODEL, OUTBOX, _call_flux

logger = logging.getLogger(__name__)

# PHOTO_OUTBOX alias for clarity inside this module.
PHOTO_OUTBOX = OUTBOX

# Style prefix that prevents face generation and keeps the scene minimal,
# photo-realistic — matches the rest of Hikari's photo aesthetic.
_SCENE_STYLE = (
    "photo-realistic, no people, no faces, no characters, no portraits. "
    "minimalist composition, natural lighting, subtle film grain, "
    "muted palette, candid object photography."
)


def _scene_for_activity(activity: str, time_phase: str, season: str) -> str:
    """Compose a scene description from the current activity + time + season.
    Returns the *subject* of the photo — _SCENE_STYLE wraps it."""
    activity = (activity or "").strip().lower()
    time_phase = (time_phase or "").strip().lower()
    season = (season or "").strip().lower()

    # Activity → object/scene mapping. Fallback to generic desk scene.
    if "model" in activity or "code" in activity or "writing" in activity:
        subject = "a laptop screen showing dense text or a graph, slightly out of focus. coffee cup half-empty on the side."
    elif "tea" in activity or "coffee" in activity or "eat" in activity:
        subject = "a hot drink in a plain ceramic mug on a wooden surface. steam visible."
    elif "read" in activity or "book" in activity:
        subject = "an open book on a table, indoor light from a side window."
    elif "walk" in activity:
        subject = "a quiet street or path, no people, late afternoon light."
    elif "music" in activity or "listen" in activity:
        subject = "headphones on a desk, a single soft light source."
    else:
        subject = "a desk corner — notebook, pen, mug. nothing posed."

    # Layer time/season subtly into the lighting.
    if "night" in time_phase or "deep_night" in time_phase:
        light = " low warm lamp light, room mostly dark."
    elif "morning" in time_phase or "drag" in time_phase:
        light = " grey morning light through a window."
    elif "evening" in time_phase or "transition" in time_phase:
        light = " late evening light, golden tones."
    else:
        light = ""

    if "winter" in season:
        ambient = " cold blue tones in the shadows, possibly a window view of bare branches."
    elif "summer" in season:
        ambient = " warm bright daylight."
    elif "autumn" in season or "fall" in season:
        ambient = " warm amber tones, dry leaves perhaps."
    elif "spring" in season:
        ambient = " soft daylight, slight green hints."
    else:
        ambient = ""

    return subject + light + ambient


def _sent_today() -> int:
    today = time.strftime("%Y-%m-%d")
    if db.runtime_get("scene_photos_sent_date") != today:
        return 0
    return db.runtime_get_int("scene_photos_sent_today", 0)


def _bump_sent() -> None:
    today = time.strftime("%Y-%m-%d")
    if db.runtime_get("scene_photos_sent_date") != today:
        db.runtime_set("scene_photos_sent_date", today)
        db.runtime_set("scene_photos_sent_today", 1)
    else:
        db.runtime_set("scene_photos_sent_today", _sent_today() + 1)


def _resolve_context() -> tuple[str, str, str]:
    """Returns (activity, time_phase, season). Reads core_blocks."""
    activity = db.get_core_block("hikari_current_activity") or "at her desk"
    time_phase = db.runtime_get("time_texture") or "default"
    cycle_state = db.get_core_block("cycle_state") or ""
    season = "default"
    if cycle_state:
        # composite_label like "luteal-late / winter / sunday / night-mode"
        parts = cycle_state.split("/")
        if len(parts) >= 2:
            season = parts[1].strip()
    return str(activity), str(time_phase), season


_DAILY_CAP = int(cfg.get("scene_photo.daily_cap", 2))


@tool(
    "scene_photo_send",
    "Send a scene photo (environment, desk, object — no face). Deliberate, "
    f"daily-capped at {_DAILY_CAP}. "
    "Call when sharing a moment of presence without a selfie — making tea, "
    "the screen you're staring at, the view from your window. Never on every "
    "turn. Returns {ok, idempotency_key, reason}.",
    {"hint": str},
    annotations=annotations_for("scene_photo_send"),
)
async def scene_photo_send(args: dict[str, Any]) -> dict[str, Any]:
    """Generate + queue a scene photo."""
    hint = (args.get("hint") or "").strip()

    cap = int(cfg.get("scene_photo.daily_cap", 2))
    if _sent_today() >= cap:
        return {"content": [{"type": "text", "text": "refused: daily_cap"}]}

    activity, time_phase, season = _resolve_context()
    if hint:
        activity = hint  # explicit hint overrides current activity

    scene_subject = _scene_for_activity(activity, time_phase, season)
    full_prompt = f"{_SCENE_STYLE} {scene_subject}"

    try:
        image_bytes = await _call_flux(full_prompt, DEFAULT_MODEL)
    except Exception:
        logger.exception("scene_photo: flux call failed")
        return {"content": [{"type": "text", "text": "refused: flux_failed"}]}

    if not image_bytes:
        return {"content": [{"type": "text", "text": "refused: empty_image"}]}

    ikey = f"scene_{uuid.uuid4().hex[:16]}"
    PHOTO_OUTBOX.mkdir(parents=True, exist_ok=True)
    img_path = PHOTO_OUTBOX / f"{ikey}.jpg"
    img_path.write_bytes(image_bytes)

    payload = {
        "path": str(img_path),
        "prompt": scene_subject,
        "kind": "scene",
        "activity": activity,
        "time_phase": time_phase,
        "season": season,
    }
    row_id = db.media_outbox_insert("photo", ikey, payload)
    if row_id is None:
        img_path.unlink(missing_ok=True)
        return {"content": [{"type": "text", "text": "refused: dedup_or_insert_failed"}]}

    _bump_sent()
    return {"content": [{"type": "text", "text": f"queued {ikey} (scene)"}]}
