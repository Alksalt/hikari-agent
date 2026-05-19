"""SPASM Egocentric Context Projection — re-render conversation history so the
model reads past turns as its own first-person memory rather than a third-person
dialog log.

Reference: arxiv 2604.09212 (ACL 2026). The paper documents a Cohen's d ≈ -0.75
reduction in emotion-drift over 18-turn conversations when role labels in
history blocks are rewritten from "user"/"assistant" (third-person dialog log
framing) to "[partner]"/"[self]" (first-person memory framing). No fine-tune
needed — purely a prompt-assembly intervention.

How we apply it
---------------
Hikari's live conversation history is managed by the Claude SDK via session_id
resume; we never re-serialize past turns into the prompt. The places where
third-person role labels DO leak into prompt material are:

  1. ``agents/handoff.format_for_injection`` — the "session handoff" block that
     replays the last N turns after a >30 min gap.
  2. ``agents/reflection.maybe_run_session_consolidation`` — the rolling
     transcript fed to the reflection LLM for episode summarization.

Both call sites pass formatted strings through ``project_egocentric`` before
they reach the model.

Config gate: ``persona.egocentric_projection`` (default ``true``). Toggle off if
it backfires.
"""

from __future__ import annotations

import re

# Patterns ordered so the most specific (longer, label-style) matches first,
# preventing partial conflicts. All patterns are anchored at line starts (or at
# common natural-language separators for the "X said" forms) so we don't rewrite
# arbitrary mid-sentence mentions of the word "user".
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # "User: ", "USER: ", "user: " at line start → "[partner]: "
    (re.compile(r"(?m)^(?:user|USER|User)\s*:\s+"), "[partner]: "),
    # "Assistant: " at line start → "[self]: "
    (re.compile(r"(?m)^(?:assistant|ASSISTANT|Assistant)\s*:\s+"), "[self]: "),
    # "Hikari: " / "Bot: " at line start → "[self]: " (we're Hikari from her POV)
    (re.compile(r"(?m)^(?:hikari|Hikari|HIKARI|bot|Bot|BOT)\s*:\s+"), "[self]: "),
    # Bracketed natural-language attributions occasionally used in summaries.
    (re.compile(r"\buser said\b", re.IGNORECASE), "[partner] said"),
    (re.compile(r"\bassistant said\b", re.IGNORECASE), "[self] said"),
)


def project_egocentric(text: str) -> str:
    """Rewrite third-person role labels into first-person ``[self]``/``[partner]``.

    Idempotent: text that already uses the egocentric labels passes through
    unchanged. Safe to call on text that contains no role labels at all (returns
    the input verbatim). Only complete word-boundary matches at line starts (or
    after common separators for the "X said" forms) are rewritten — body content
    is preserved.

    Reference: arxiv 2604.09212 (ACL 2026), Cohen's d ≈ -0.75 on emotion drift.
    """
    if not text:
        return text
    out = text
    for pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    return out


def is_enabled() -> bool:
    """Return True when the projection is active. Off-switch lives in
    ``config/engagement.yaml`` under ``persona.egocentric_projection``."""
    try:
        from . import config as cfg
        return bool(cfg.get("persona.egocentric_projection", True))
    except Exception:
        # If config can't load, default to ON — the projection is the documented
        # research-backed behavior; failing closed defeats the point.
        return True


def maybe_project(text: str) -> str:
    """Apply ``project_egocentric`` iff the config gate is enabled. Convenience
    wrapper for callers that want the gate-aware version without duplicating the
    check at every join point."""
    if not text:
        return text
    if not is_enabled():
        return text
    return project_egocentric(text)
