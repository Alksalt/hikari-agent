"""Politeness gate — rude-tone detection on inbound messages.

Hikari helps because she chooses to. If asked rudely (commanding, dismissive,
insulting), she refuses on that turn. Politely-retried, she helps normally —
no extra warmth as a reward.

Two paths the bridge can take:

1. **Fast path (deterministic)** — ``is_rude(text)`` regex check. If True, the
   bridge short-circuits with a random ``refusal_phrase`` from config, logs to
   character_thoughts, never calls the LLM. Cheap, consistent.

2. **Soft path (LLM judgment)** — the rude_instruction in config can be passed
   as an additional system context block via the hook, letting Hikari decide
   tone-based subtleties the regex misses. Not wired by default — fast path
   is enough for the obvious cases.

Both paths are character, not safety. This is not a refusal of the *content*
of the request — it's a refusal of the *manner*. If the same content comes in
politely, Hikari helps.
"""

from __future__ import annotations

import logging
import random
import re

from . import config as cfg

logger = logging.getLogger(__name__)

_RUDE_PATTERNS: list[re.Pattern[str]] | None = None


def _patterns() -> list[re.Pattern[str]]:
    global _RUDE_PATTERNS
    if _RUDE_PATTERNS is None:
        raw = cfg.get("politeness_gate.rude_patterns") or []
        _RUDE_PATTERNS = [re.compile(p) for p in raw]
    return _RUDE_PATTERNS


def reload_patterns() -> None:
    """Drop the compiled-pattern cache. Use after ``config.reload()``."""
    global _RUDE_PATTERNS
    _RUDE_PATTERNS = None


def is_enabled() -> bool:
    return bool(cfg.get("politeness_gate.enabled", True))


def is_rude(text: str) -> tuple[bool, str | None]:
    """Return (rude, matched_pattern_string_or_None).

    Conservative: requires an explicit rude/commanding pattern to match. Bare
    one-word commands (e.g. "help") do NOT count — only the patterns listed
    in config. This keeps false positives low on a one-user bot where the user
    sometimes types brusquely without being rude.
    """
    if not is_enabled() or not text or not text.strip():
        return False, None
    for pat in _patterns():
        m = pat.search(text)
        if m:
            return True, m.group(0)
    return False, None


def random_refusal() -> str:
    pool = cfg.get("politeness_gate.refusal_phrases") or ["ask again. nicely."]
    return random.choice(pool)
