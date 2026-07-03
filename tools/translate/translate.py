"""``translate`` — pick a backend (DeepL preferred, LibreTranslate fallback)."""
from __future__ import annotations

import logging
import os
from typing import Any

from claude_agent_sdk import tool

from agents import config as cfg
from tools._annotations import annotations_for
from tools._response import ok as _ok
from tools.translate._shared import (
    _SUPPORTED,
    _deepl_translate,
    _libretranslate,
    _romanize_japanese,
)

logger = logging.getLogger(__name__)


@tool(
    "translate",
    "Translate text. target one of {ru, en, uk, no, ja, ja_romaji}. ja_romaji "
    "returns Japanese AND a romaji (Hepburn) transliteration so the user can read "
    "it without kana. Uses DeepL Free (if DEEPL_API_KEY set) else LibreTranslate.",
    {"text": str, "target": str, "source": str},
    annotations=annotations_for("translate"),
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
    preferred = str(cfg.get("translate.preferred_backend", "deepl")).strip().lower()
    backend_order = (
        ["libretranslate", "deepl"] if preferred == "libretranslate" else ["deepl", "libretranslate"]
    )

    result = None
    backend = ""
    for backend in backend_order:
        result = (
            await _deepl_translate(text, deepl_target)
            if backend == "deepl"
            else await _libretranslate(text, deepl_target)
        )
        if result is not None:
            break
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
