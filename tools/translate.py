"""Phase 10: translation tool.

Backends:
  - DeepL Free (preferred, requires DEEPL_API_KEY)
  - LibreTranslate (free public endpoint fallback)

Targets: {ru, en, uk, no, ja, ja_romaji}. ja_romaji adds pykakasi
transliteration after the Japanese translation.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from claude_agent_sdk import tool

from agents import config as cfg
from tools._response import ok as _ok

logger = logging.getLogger(__name__)


_DEEPL_LANG_MAP = {"ru": "RU", "en": "EN-US", "uk": "UK", "no": "NB", "ja": "JA"}


async def _deepl_translate(text: str, target: str) -> tuple[str, str] | None:
    api_key = os.environ.get("DEEPL_API_KEY")
    if not api_key:
        return None
    deepl_target = _DEEPL_LANG_MAP.get(target)
    if not deepl_target:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://api-free.deepl.com/v2/translate",
                data={"text": text, "target_lang": deepl_target},
                headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
            )
            r.raise_for_status()
            data = r.json() or {}
            translations = data.get("translations") or []
            if not translations:
                return None
            t = translations[0]
            return t.get("text", ""), (t.get("detected_source_language") or "").lower()
    except Exception:
        logger.exception("deepl translate failed")
        return None


async def _libretranslate(text: str, target: str) -> tuple[str, str] | None:
    endpoint = str(cfg.get(
        "translate.libretranslate_endpoint",
        "https://libretranslate.com/translate",
    ))
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                endpoint,
                json={"q": text, "source": "auto", "target": target, "format": "text"},
            )
            r.raise_for_status()
            data = r.json() or {}
            translated = data.get("translatedText", "")
            if not translated:
                return None
            return translated, (data.get("detectedLanguage") or {}).get("language", "")
    except Exception:
        logger.exception("libretranslate failed")
        return None


def _romanize_japanese(text: str) -> str | None:
    try:
        import pykakasi
        kks = pykakasi.kakasi()
        result = kks.convert(text)
        return " ".join(r["hepburn"] for r in result).strip()
    except Exception:
        logger.exception("pykakasi romaji failed")
        return None


_SUPPORTED = {"ru", "en", "uk", "no", "ja", "ja_romaji"}


@tool(
    "translate",
    "Translate text. target one of {ru, en, uk, no, ja, ja_romaji}. ja_romaji "
    "returns Japanese AND a romaji (Hepburn) transliteration so the user can read "
    "it without kana. Uses DeepL Free (if DEEPL_API_KEY set) else LibreTranslate.",
    {"text": str, "target": str, "source": str},
)
async def translate(args: dict[str, Any]) -> dict[str, Any]:
    text = (args.get("text") or "").strip()
    target = (args.get("target") or "").strip().lower()
    if not text:
        return _ok("refused: empty text")
    if target not in _SUPPORTED:
        return _ok(
            f"refused: target={target!r} unsupported. Supported: "
            f"{', '.join(sorted(_SUPPORTED))}"
        )

    # Short-circuit: if DEEPL_API_KEY is absent AND the configured LibreTranslate
    # endpoint is still the default public libretranslate.com (which requires auth
    # since late 2023 and returns 403), refuse immediately rather than burning a
    # network round-trip on a guaranteed failure.
    _lt_endpoint = str(cfg.get(
        "translate.libretranslate_endpoint",
        "https://libretranslate.com/translate",
    ))
    _public_lt = "libretranslate.com"
    if not os.environ.get("DEEPL_API_KEY") and _public_lt in _lt_endpoint:
        return _ok("refused: translation backend not configured (set DEEPL_API_KEY)")

    deepl_target = "ja" if target == "ja_romaji" else target
    result = await _deepl_translate(text, deepl_target)
    backend = "deepl"
    if result is None:
        result = await _libretranslate(text, deepl_target)
        backend = "libretranslate"
    if result is None:
        return _ok("translation failed: all backends exhausted")

    translated, detected = result
    transliteration: str | None = None
    if target == "ja_romaji":
        transliteration = _romanize_japanese(translated)
    summary_lines = [f"{detected or '??'} -> {target}: {translated}"]
    if transliteration:
        summary_lines.append(f"romaji: {transliteration}")
    return _ok(
        "\n".join(summary_lines),
        data={
            "translated_text": translated,
            "detected_source": detected,
            "transliteration": transliteration,
            "backend": backend,
        },
    )


ALL_TOOLS = [translate]
