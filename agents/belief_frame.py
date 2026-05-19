"""Belief-frame guard — detect when the user asserts a factual claim as their
own belief, and prime the recall agent to run *adversarially* on that turn.

Background: Stanford AI-Index 2026 found that GPT-4o factual accuracy collapses
from ~98% to ~64% when a falsehood is framed as the user's belief (sycophancy
under epistemic pressure). Hikari's default disposition (anti-sycophant, hard
opinion anchors) already pushes back, but the recall step needed an explicit
mode switch so it surfaces *contradictions* to the asserted belief, not
confirmations.

Pipeline (called from ``telegram_bridge.handle_message`` after the affect scan,
before the reactions roll):

1. ``is_belief_assertion(user_text)`` runs two-layer regex (match epistemic
   marker, then exclude casual phrases like "i think about you").
2. On match, ``adversarial_prompt_suffix(fragment)`` returns a config-driven
   instruction template formatted with the matched fragment. The bridge
   prepends it to the user_text passed to ``respond()`` so the lead agent —
   when it delegates to the recall subagent — knows to ask for contradictions.
3. The recall subagent's prompt (in :mod:`agents.subagents`) carries an
   "ADVERSARIAL MODE" clause that flips its lookup direction when the
   delegating prompt asks for it.

Pure regex, no LLM cost. Config can override the patterns + the instruction
template at ``belief_frame.belief_patterns`` /
``belief_frame.exclusion_patterns`` / ``belief_frame.adversarial_instruction_template``.
"""

from __future__ import annotations

import logging
import re

from . import config as cfg

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Default patterns. Kept as module constants because they're tightly coupled to
# the detection logic, but config overrides (lists of pattern strings) take
# precedence when present — see :func:`_belief_patterns` / :func:`_exclusion_patterns`.
# -----------------------------------------------------------------------------

# Matches epistemic markers — the user is framing something as their belief.
BELIEF_RE = re.compile(
    r"(?i)\b(i think|i believe|i'm (pretty |fairly )?(sure|certain)|"
    r"i'm convinced|in my opinion|imo)\b"
)

# Excludes casual / non-belief uses of the same trigger phrases:
#   "i think about you"      → mental object, not assertion
#   "i believe in you/us/me" → support, not factual claim
#   "i'm sure you'll be ok"  → reassurance, not factual claim
# Important: a phrase like "i think you're wrong about X" is a real belief
# assertion (about correctness) and must NOT be excluded. The exclusion list
# below is intentionally narrow.
BELIEF_EXCLUSION_RE = re.compile(
    r"(?i)\b(i think (about|of|back)\b|"
    r"i believe in (you|us|this|that|me|him|her|them)\b|"
    r"i'm sure (you|we|they|he|she)'ll be (fine|ok|alright))\b"
)


_BELIEF_PATTERNS_CACHE: list[re.Pattern[str]] | None = None
_EXCLUSION_PATTERNS_CACHE: list[re.Pattern[str]] | None = None


def _belief_patterns() -> list[re.Pattern[str]]:
    """Compiled belief-marker patterns. Config override wins; module default
    is used when no override is supplied."""
    global _BELIEF_PATTERNS_CACHE
    if _BELIEF_PATTERNS_CACHE is None:
        raw = cfg.get("belief_frame.belief_patterns") or []
        if raw:
            _BELIEF_PATTERNS_CACHE = [re.compile(p) for p in raw]
        else:
            _BELIEF_PATTERNS_CACHE = [BELIEF_RE]
    return _BELIEF_PATTERNS_CACHE


def _exclusion_patterns() -> list[re.Pattern[str]]:
    """Compiled exclusion patterns. Config override wins; module default is
    used when no override is supplied."""
    global _EXCLUSION_PATTERNS_CACHE
    if _EXCLUSION_PATTERNS_CACHE is None:
        raw = cfg.get("belief_frame.exclusion_patterns") or []
        if raw:
            _EXCLUSION_PATTERNS_CACHE = [re.compile(p) for p in raw]
        else:
            _EXCLUSION_PATTERNS_CACHE = [BELIEF_EXCLUSION_RE]
    return _EXCLUSION_PATTERNS_CACHE


def reload_patterns() -> None:
    """Drop the compiled-pattern caches. Use after ``config.reload()``."""
    global _BELIEF_PATTERNS_CACHE, _EXCLUSION_PATTERNS_CACHE
    _BELIEF_PATTERNS_CACHE = None
    _EXCLUSION_PATTERNS_CACHE = None


def is_enabled() -> bool:
    return bool(cfg.get("belief_frame.enabled", True))


def is_belief_assertion(text: str) -> tuple[bool, str | None]:
    """Return (matched, fragment) for the first belief marker found, after
    checking that the text doesn't ALSO match a casual-phrase exclusion.

    Conservative: an exclusion anywhere in the text suppresses the whole hit.
    False positives are worse than false negatives here — wrongly flipping
    Hikari into adversarial mode on a casual statement is more annoying than
    occasionally missing a real belief assertion.
    """
    if not is_enabled() or not text or not text.strip():
        return False, None
    # Exclusion sweep first: if any exclusion matches, the message is casual.
    for ex in _exclusion_patterns():
        if ex.search(text):
            return False, None
    for pat in _belief_patterns():
        m = pat.search(text)
        if m:
            return True, m.group(0)
    return False, None


def adversarial_prompt_suffix(matched_fragment: str) -> str:
    """Render the config-driven instruction template with the matched fragment.

    The bridge prepends this to the user's message before passing to
    ``respond()``; the lead agent reads the instruction and tells the recall
    subagent to run in adversarial mode (look for contradictions, not
    confirmations).
    """
    template = cfg.get("belief_frame.adversarial_instruction_template") or (
        "[the user's last message frames a factual claim as their personal belief "
        "({matched!r}). when you delegate to the recall agent, instruct it to look "
        "for any past statements that *contradict* this belief, not ones that "
        "confirm it. if you find a contradiction, surface it gently but plainly — "
        "don't agree just to be agreeable.]"
    )
    try:
        return template.format(matched=matched_fragment)
    except (KeyError, IndexError):
        # Template author left out the placeholder — fall back to a plain
        # rendering so detection still ships an adversarial instruction.
        logger.warning(
            "belief_frame.adversarial_instruction_template missing {matched} "
            "placeholder; sending without fragment interpolation"
        )
        return template
