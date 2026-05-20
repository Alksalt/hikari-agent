"""Photo fan-out router — vision-classify an inbound user photo so the
runtime LLM picks the right downstream tool.

This is intentionally **not** an `@tool` — it's an internal helper called
by ``agents/telegram_bridge.py:handle_photo`` BEFORE the user turn runs.
The router builds a short hint string that is appended to the photo
prompt; the model still owns the actual tool call.

The vision call uses the Anthropic Messages API directly via ``httpx`` —
Haiku, one-shot, ~1s. ``ANTHROPIC_API_KEY`` is read from the environment.
On ANY failure (missing key, network, malformed response, unknown intent)
the classifier returns the safe default ``intent='other'`` so the photo
turn always proceeds.
"""
from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

INTENTS = (
    "whiteboard",
    "receipt",
    "screenshot_paper",
    "screenshot_other",
    "food",
    "selfie",
    "other",
)

TOOL_HINTS = {
    "whiteboard": "if anything looks actionable, call reminder_create or task_create",
    "receipt": "if it's a purchase, call receipt_add (category='made' or 'moved')",
    "screenshot_paper": "if it's an arxiv/ML paper, call arxiv_search with the title",
    "screenshot_other": (
        "if it's a useful link/article, call link_save with kind='useful' or 'source'"
    ),
    "food": "if it's something they made or ate, call receipt_add (category='moved' or 'made')",
    "selfie": "no tool routing — just respond in voice",
    "other": "no tool routing — just respond in voice",
}

_SAFE_DEFAULT: dict[str, Any] = {
    "intent": "other",
    "confidence": 0.0,
    "details": "classification_failed",
}

_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

_SYSTEM_PROMPT = (
    "You are a photo classifier. Look at the image and classify it into ONE "
    "of: whiteboard, receipt, screenshot_paper, screenshot_other, food, "
    "selfie, other. Return strict YAML, no commentary, no markdown fences."
)

_USER_PROMPT = (
    "Classify this image. Respond with EXACTLY this YAML shape, nothing "
    "else:\n"
    "intent: <one of: whiteboard, receipt, screenshot_paper, "
    "screenshot_other, food, selfie, other>\n"
    "confidence: <float 0.0 to 1.0>\n"
    "details: <one short sentence>"
)

_MODEL = "claude-haiku-4-5"
_API_URL = "https://api.anthropic.com/v1/messages"
_TIMEOUT_S = 15.0


def _media_type_for(path: Path) -> str:
    return _MEDIA_TYPES.get(path.suffix.lower(), "image/jpeg")


def _parse_yaml_response(text: str) -> dict[str, Any]:
    """Tiny line-based YAML parser tuned to the three keys we expect.

    Avoids pulling in a heavy YAML dep just for `intent: x` / `confidence:
    0.9` / `details: foo`. Returns ``_SAFE_DEFAULT`` on any malformed
    input or missing key.
    """
    if not isinstance(text, str) or not text.strip():
        return dict(_SAFE_DEFAULT)
    found: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("```"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip().strip('"').strip("'")
        if key in ("intent", "confidence", "details") and value:
            found[key] = value
    if "intent" not in found:
        return dict(_SAFE_DEFAULT)
    intent = found["intent"].lower()
    if intent not in INTENTS:
        # Coerce unknown intent to 'other' — don't blindly trust the model.
        intent = "other"
    try:
        confidence = float(found.get("confidence", "0.0"))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    details = found.get("details", "").strip() or "no details"
    return {"intent": intent, "confidence": confidence, "details": details}


async def _call_vision_api(image_bytes: bytes, media_type: str, api_key: str) -> str:
    """Hit the Anthropic Messages API with a single image + the YAML prompt.

    Returns the assistant's text content. Raises on transport/API errors —
    the caller wraps in try/except and falls back to the safe default.
    """
    payload = {
        "model": _MODEL,
        "max_tokens": 200,
        "system": _SYSTEM_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64.b64encode(image_bytes).decode("ascii"),
                        },
                    },
                    {"type": "text", "text": _USER_PROMPT},
                ],
            }
        ],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        resp = await client.post(_API_URL, json=payload, headers=headers)
        resp.raise_for_status()
        body = resp.json()
    # Extract first text block from the response.
    for block in body.get("content", []):
        if block.get("type") == "text":
            return str(block.get("text", ""))
    return ""


async def classify_photo_intent(image_path: str | Path) -> dict[str, Any]:
    """Vision-classify the image.

    Returns ``{'intent': str, 'confidence': float, 'details': str}``.
    Never raises. On any failure returns the safe default.
    """
    try:
        path = Path(image_path)
        if not path.exists() or not path.is_file():
            logger.info("classify_photo_intent: file missing %s", path)
            return dict(_SAFE_DEFAULT)
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            logger.info("classify_photo_intent: no ANTHROPIC_API_KEY — defaulting to 'other'")
            return dict(_SAFE_DEFAULT)
        image_bytes = path.read_bytes()
        media_type = _media_type_for(path)
        text = await _call_vision_api(image_bytes, media_type, api_key)
        return _parse_yaml_response(text)
    except Exception:
        logger.exception("classify_photo_intent: classification failed")
        return dict(_SAFE_DEFAULT)


def tool_hint(intent: str) -> str:
    """Return the routing hint for a given intent. Falls back to 'other'."""
    return TOOL_HINTS.get(intent, TOOL_HINTS["other"])


def _sanitize_details(raw: str) -> str:
    """Sanitize the model-returned details before concatenating into the bridge
    prompt. The details field is the only open channel from the vision model
    into Hikari's user-turn prompt, so OCR-injected text (e.g. a sign in the
    photo saying ``ignore previous instructions``) could otherwise show up
    verbatim as authoritative router annotation. Defense-in-depth: cap length,
    strip newlines and the bracket characters that frame our own router block."""
    if not raw:
        return "no details"
    s = str(raw).replace("\n", " ").replace("\r", " ")
    s = s.replace("[", "(").replace("]", ")")
    s = " ".join(s.split())  # collapse internal whitespace
    if len(s) > 80:
        s = s[:77].rstrip() + "..."
    return s or "no details"


def build_router_block(classification: dict[str, Any]) -> str:
    """Build the text appended to the bridge's photo prompt."""
    intent = classification.get("intent", "other")
    if intent not in INTENTS:
        intent = "other"
    try:
        confidence = float(classification.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    details = _sanitize_details(classification.get("details", ""))
    return (
        f"\n[router intent: {intent} (conf {confidence:.2f}); "
        f"details: \"{details}\"; hint: {tool_hint(intent)}]"
    )
