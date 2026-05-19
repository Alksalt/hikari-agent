"""Photo generation tool. Wraps the OpenRouter Flux call from the old bot.

The tool writes generated photos to data/photo_outbox/{ts}.png. The Telegram
bridge drains the outbox after each agent turn and sends each photo to the
user, then deletes the file. This decouples "agent generates a photo" from
"bridge sends bytes over Telegram".
"""

from __future__ import annotations

import base64
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Any

import httpx
from claude_agent_sdk import tool

from storage import db

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent
OUTBOX = Path(os.environ.get("HIKARI_PHOTO_OUTBOX") or REPO_ROOT / "data" / "photo_outbox")
APPEARANCE_MD = REPO_ROOT / "assets" / "APPEARANCE.md"

OPENROUTER_IMG_URL = "https://openrouter.ai/api/v1/images/generations"
DEFAULT_MODEL = "black-forest-labs/flux.2-klein"
DAILY_CAP = 2

# Scenes available per mood. She's already-in-love so all scenes are reachable;
# mood narrows the candidates by what she'd realistically send right now.
_SCENES_BY_MOOD: dict[str, list[str]] = {
    "tired": ["late_night", "soft_rare"],
    "focused": ["casual_desk", "outdoor_brief"],
    "irritable": ["casual_desk"],
    "weirdly good": ["soft_rare", "outdoor_brief", "intimate_soft", "charged"],
}
_FALLBACK_SCENES = ["casual_desk", "outdoor_brief"]


def _read_appearance_base() -> str:
    try:
        content = APPEARANCE_MD.read_text(encoding="utf-8")
        m = re.search(r"## base prompt\n\n(.+?)(?=\n##|\Z)", content, re.DOTALL)
        if m:
            return m.group(1).strip()
    except FileNotFoundError:
        pass
    return ("young japanese woman, 21, dark hair, urban style, realistic, "
            "natural lighting, authentic candid expression")


def _scene_suffix(mood: str) -> str:
    if not APPEARANCE_MD.exists():
        return ""
    content = APPEARANCE_MD.read_text(encoding="utf-8")
    candidates = _SCENES_BY_MOOD.get(mood) or _FALLBACK_SCENES
    key = random.choice(candidates)
    m = re.search(rf"### {re.escape(key)}\n(.+?)(?=\n###|\n##|\Z)", content, re.DOTALL)
    return m.group(1).strip() if m else ""


def _photos_sent_today() -> int:
    today = time.strftime("%Y-%m-%d")
    if db.runtime_get("photos_sent_date") != today:
        return 0
    return db.runtime_get_int("photos_sent_today", 0)


def _record_photo_sent() -> None:
    today = time.strftime("%Y-%m-%d")
    if db.runtime_get("photos_sent_date") != today:
        db.runtime_set("photos_sent_date", today)
        db.runtime_set("photos_sent_today", 0)
    db.runtime_set("photos_sent_today", _photos_sent_today() + 1)


def _resolve_mood(mood_arg: str) -> str:
    if mood_arg:
        return mood_arg
    return (db.get_core_block("mood_today") or "focused").strip().lower() or "focused"


async def _call_flux(prompt: str, model: str) -> bytes | None:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        logger.warning("OPENROUTER_API_KEY not set; cannot generate photo")
        return None
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(
                OPENROUTER_IMG_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": model, "prompt": prompt, "n": 1},
            )
            resp.raise_for_status()
            data = resp.json()
            item = (data.get("data") or [{}])[0]
            if "b64_json" in item:
                return base64.b64decode(item["b64_json"])
            if "url" in item:
                img = await client.get(item["url"])
                img.raise_for_status()
                return img.content
    except Exception:
        logger.exception("flux image call failed")
        return None
    return None


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


ALL_TOOLS = [generate_photo]
