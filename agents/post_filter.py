"""Outgoing-message filters that defend Hikari's persona before send.

Three passes, all driven by ``config/engagement.yaml``:

  0. **Canary leak detector** — if the outbound text contains the per-install
     injection canary token, refuse to send and log a CRITICAL alert. The
     canary is only ever placed inside ``wrap_untrusted`` blocks (see
     ``agents.injection_guard``); finding it outbound means an attacker's
     untrusted-content block bypassed the LLM's data/instruction boundary.
     Replaces the outbound message with a curt "..." instead of shipping
     potentially-exfiltrated content.

1. **Refusal-voice filter** — catches Claude's default assistant patter
   ("I cannot help with that as an AI", "I'd be happy to assist") that leaks
   under safety pressure. Character.AI's #1 retention killer in 2025-26 was
   safety-officer voice. Detected matches are either short-replaced with an
   in-voice phrase or LLM-rewritten in Hikari's voice.

2. **Sycophancy guard** — Science Mar 2026 showed memory-having models drift
   toward agreement. Detects collapse patterns ("you're right", "good point",
   "I agree completely") and "anchor violation" patterns where Hikari
   wholesale concedes one of her hard opinion anchors. On hit, returns a
   rewrite instruction the caller can use to ask the LLM to redo the reply.

Both filters are deterministic regex passes — no LLM cost on the hot path.
The caller decides whether to short-replace, rewrite, or escalate.
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass

from . import config as cfg

logger = logging.getLogger(__name__)


# ---------- compiled-pattern caches ----------

_PATTERN_CACHE: dict[str, list[re.Pattern[str]]] = {}


def _compiled(key: str, source_path: str) -> list[re.Pattern[str]]:
    """Compile-cache regex lists from config. ``key`` is a cache key; ``source_path``
    is the dot-path in config that holds the raw pattern list."""
    if key not in _PATTERN_CACHE:
        raw = cfg.get(source_path) or []
        _PATTERN_CACHE[key] = [re.compile(p) for p in raw]
    return _PATTERN_CACHE[key]


def reload_patterns() -> None:
    """Drop the compiled-pattern cache. Call after ``config.reload()`` in tests."""
    _PATTERN_CACHE.clear()


# ---------- refusal-voice filter ----------

@dataclass
class RefusalCheck:
    """Result of the refusal-voice scan."""
    matched: bool
    matches: list[str]
    should_short_replace: bool
    replacement: str | None = None


def scan_refusal_voice(text: str) -> RefusalCheck:
    """Return whether the message contains assistant-voice patter that breaks
    Hikari's character, and whether a short replacement is appropriate.

    Short-replace fires only when BOTH:
      - the message is short (≤ ``refusal_filter.rewrite_threshold_chars``), AND
      - the longest match covers at least ``short_replace_match_fraction`` of the
        message length (i.e. the banned phrase dominates — the rest is connective).

    This avoids discarding a legit short Hikari reply that happens to contain a
    banned token verbatim. Longer or dilute matches return
    ``should_short_replace=False`` — the caller is expected to request an LLM
    rewrite or just log and let it ship (filter-only is the default).
    """
    if not cfg.get("refusal_filter") or not text:
        return RefusalCheck(matched=False, matches=[], should_short_replace=False)

    patterns = _compiled("refusal", "refusal_filter.banned_patterns")
    hits: list[str] = []
    for pat in patterns:
        m = pat.search(text)
        if m:
            hits.append(m.group(0))

    if not hits:
        return RefusalCheck(matched=False, matches=[], should_short_replace=False)

    threshold = int(cfg.get("refusal_filter.rewrite_threshold_chars", 80))
    fraction_required = float(cfg.get("refusal_filter.short_replace_match_fraction", 0.35))
    longest_hit_len = max(len(h) for h in hits)
    dominates = (longest_hit_len / max(len(text), 1)) >= fraction_required
    short = len(text) <= threshold and dominates

    replacement = None
    if short:
        pool = cfg.get("refusal_filter.short_replacements") or ["..."]
        replacement = random.choice(pool)
    return RefusalCheck(
        matched=True,
        matches=hits,
        should_short_replace=short,
        replacement=replacement,
    )


# ---------- sycophancy guard ----------

@dataclass
class SycophancyCheck:
    """Result of the sycophancy scan."""
    triggered: bool
    collapse_count: int
    anchor_violations: list[str]
    rewrite_instruction: str | None = None


def scan_sycophancy(text: str) -> SycophancyCheck:
    """Return whether the reply collapsed too agreeably or violated an opinion anchor.

    Two signals:
      - ``collapse_phrases`` — agreement leakage; count must exceed
        ``max_collapses_per_reply``.
      - ``anchor_violations`` — patterns that represent surrendering a position
        Hikari is supposed to hold (e.g. admitting she needs people).

    Any anchor violation triggers a rewrite. Collapses above threshold trigger
    too. The caller should re-prompt the agent with ``rewrite_instruction``
    prepended to the prior turn's context.
    """
    if not cfg.get("sycophancy_guard.enabled", True) or not text:
        return SycophancyCheck(triggered=False, collapse_count=0, anchor_violations=[])

    collapses = _compiled("syc_collapse", "sycophancy_guard.collapse_phrases")
    anchors = _compiled("syc_anchors", "sycophancy_guard.anchor_violations")

    collapse_count = sum(1 for pat in collapses if pat.search(text))
    violations = [pat.pattern for pat in anchors if pat.search(text)]

    max_collapses = int(cfg.get("sycophancy_guard.max_collapses_per_reply", 1))
    triggered = (collapse_count > max_collapses) or bool(violations)
    instruction = (
        cfg.get("sycophancy_guard.rewrite_instruction") if triggered else None
    )
    return SycophancyCheck(
        triggered=triggered,
        collapse_count=collapse_count,
        anchor_violations=violations,
        rewrite_instruction=instruction,
    )


# ---------- combined entry point ----------

@dataclass
class FilterResult:
    """Final result of the combined outgoing-message filter pass."""
    text: str                      # text to actually send (possibly rewritten)
    refusal_short_replaced: bool   # True if we swapped the message wholesale
    refusal_hits: list[str]
    sycophancy_triggered: bool
    sycophancy_violations: list[str]
    needs_llm_rewrite: bool        # caller should re-prompt the LLM
    rewrite_instruction: str | None


def filter_outgoing(text: str) -> FilterResult:
    """Run all filters. Cheap to call on every outbound message.

    Caller contract:
      - if ``refusal_short_replaced`` is True, ``text`` is already replaced; send as-is.
      - if ``needs_llm_rewrite`` is True, re-prompt the agent with
        ``rewrite_instruction`` and call ``filter_outgoing`` again on the new text.
      - otherwise just send ``text``.
    """
    # Canary leak check runs first — catastrophic, never let through.
    try:
        from agents.injection_guard import outbound_contains_canary
        if outbound_contains_canary(text):
            logger.critical(
                "post_filter: CANARY LEAK in outbound message — blocked. "
                "len=%d preview=%r", len(text), text[:120],
            )
            return FilterResult(
                text="...",
                refusal_short_replaced=True,
                refusal_hits=["canary_leak"],
                sycophancy_triggered=False,
                sycophancy_violations=[],
                needs_llm_rewrite=False,
                rewrite_instruction=None,
            )
    except Exception:  # noqa: BLE001
        logger.exception("canary check failed (non-fatal)")

    refusal = scan_refusal_voice(text)
    sycophancy = scan_sycophancy(text)

    out_text = text
    short_replaced = False
    needs_rewrite = False
    rewrite_instruction = None

    if refusal.matched and refusal.should_short_replace and refusal.replacement:
        out_text = refusal.replacement
        short_replaced = True
        logger.info(
            "post_filter: refusal-voice short-replaced (hits=%s)",
            refusal.matches[:3],
        )
    elif refusal.matched and not refusal.should_short_replace:
        # Only request a real LLM rewrite if explicitly opted in via config.
        # Default is detect-and-log; the safety-voice line ships but daily
        # reflection sees the trigger in character_thoughts.
        if cfg.get("refusal_filter.enable_llm_rewrite", False):
            needs_rewrite = True
            rewrite_instruction = (
                "[your last reply leaked assistant-safety voice "
                f"(matched: {refusal.matches[:3]}). rewrite it in Hikari's voice. "
                "if you genuinely don't want to do something, refuse like a person: "
                "short, dry, no AI-disclaimer language. drop 'as an AI', 'I cannot', "
                "'I'd be happy to', 'great question'. she would never say those.]"
            )

    if sycophancy.triggered:
        if cfg.get("sycophancy_guard.enable_llm_rewrite", False):
            needs_rewrite = True
            sycophancy_instr = sycophancy.rewrite_instruction or ""
            rewrite_instruction = (
                f"{rewrite_instruction or ''}\n\n{sycophancy_instr}".strip()
            )
        logger.info(
            "post_filter: sycophancy triggered (collapses=%d, violations=%d)",
            sycophancy.collapse_count, len(sycophancy.anchor_violations),
        )

    # Short-replacement supersedes rewrite — if we already swapped to a curt phrase,
    # there's nothing to rewrite.
    if short_replaced:
        needs_rewrite = False
        rewrite_instruction = None

    return FilterResult(
        text=out_text,
        refusal_short_replaced=short_replaced,
        refusal_hits=refusal.matches,
        sycophancy_triggered=sycophancy.triggered,
        sycophancy_violations=sycophancy.anchor_violations,
        needs_llm_rewrite=needs_rewrite,
        rewrite_instruction=rewrite_instruction,
    )
