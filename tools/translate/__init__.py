"""Translate feature — manifest.

One handler (``translate.py``) plus backend helpers in ``_shared.py``.

Re-exports the backend helpers and constants so tests (or future tools)
can import them via the package namespace, and the ``translate`` tool
callable for the registry.
"""
from __future__ import annotations

from tools.translate._shared import (  # noqa: F401 — re-export for tests / namespace access
    _DEEPL_LANG_MAP,
    _SUPPORTED,
    _deepl_translate,
    _libretranslate,
    _romanize_japanese,
)
from tools.translate.translate import translate

ALL_TOOLS = [translate]
