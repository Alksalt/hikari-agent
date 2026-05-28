"""Shared helpers + constants for the photos feature.

Wraps the OpenRouter Flux image-generation call from the legacy bot. The
generator writes bytes to ``data/photo_outbox/{ts}.png``; the Telegram
bridge drains that outbox after every agent turn, sends the file to the
user, and deletes it. This decouples "agent decides to send a photo"
from "bytes leave the process over Telegram".

Constants (``REPO_ROOT``, ``OUTBOX``, ``APPEARANCE_MD``,
``OPENROUTER_IMG_URL``, ``DEFAULT_MODEL``, ``DAILY_CAP``,
``_SCENES_BY_MOOD``, ``_FALLBACK_SCENES``) and the private helpers
(``_read_appearance_base``, ``_scene_suffix``, ``_photos_sent_today``,
``_record_photo_sent``, ``_resolve_mood``, ``_call_flux``) are re-exported
from ``tools/photos/__init__.py`` so other modules and tests that pull
them via the package namespace keep working.
"""
from __future__ import annotations

import base64
import logging
import os
import random
import re
import time
from pathlib import Path

import httpx

from storage import db

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OUTBOX = Path(os.environ.get("HIKARI_PHOTO_OUTBOX") or REPO_ROOT / "data" / "photo_outbox")
APPEARANCE_MD = REPO_ROOT / "assets" / "APPEARANCE.md"

OPENROUTER_IMG_URL = "https://openrouter.ai/api/v1/images/generations"

def _load_cfg() -> dict:
    try:
        from agents import config as _cfg
        return {
            "default_model": str(_cfg.get("photos.default_model", "black-forest-labs/flux.2-klein")),
            "daily_cap": int(_cfg.get("photos.daily_cap", 2)),
        }
    except Exception:
        return {"default_model": "black-forest-labs/flux.2-klein", "daily_cap": 2}

_PHOTOS_CFG = _load_cfg()
DEFAULT_MODEL = _PHOTOS_CFG["default_model"]
DAILY_CAP = _PHOTOS_CFG["daily_cap"]

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
