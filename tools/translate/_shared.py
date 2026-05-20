"""Shared helpers for the translate tool.

Two backend providers (DeepL Free, LibreTranslate) plus a pykakasi-based
romaji transliteration helper. Heavy network deps (``httpx``) are
imported inside each function body — the manifest never pulls them at
import time, matching the lazy-import convention in ``tools/README.md``.
``pykakasi`` and ``requests`` were already lazy in the original module
and stay that way.
"""
from __future__ import annotations

import logging
import os

from agents import config as cfg

logger = logging.getLogger(__name__)


# DeepL uses its own ISO-ish target codes — map our normalized targets
# to what the API expects. ``ja_romaji`` is handled upstream by routing
# through ``ja`` then transliterating; it doesn't need a DeepL code.
_DEEPL_LANG_MAP = {"ru": "RU", "en": "EN-US", "uk": "UK", "no": "NB", "ja": "JA"}

_SUPPORTED = {"ru", "en", "uk", "no", "ja", "ja_romaji"}


async def _deepl_translate(text: str, target: str) -> tuple[str, str] | None:
    import httpx  # noqa: PLC0415 — lazy: network dep only loaded when this backend runs

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
    import httpx  # noqa: PLC0415 — lazy: network dep only loaded when this backend runs

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
        import pykakasi  # noqa: PLC0415 — lazy: optional dep, only loaded for ja_romaji
        kks = pykakasi.kakasi()
        result = kks.convert(text)
        return " ".join(r["hepburn"] for r in result).strip()
    except Exception:
        logger.exception("pykakasi romaji failed")
        return None
