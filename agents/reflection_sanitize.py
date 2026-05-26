"""Validate values that reflection wants to write into high-priority memory
surfaces (core_blocks, peer_model, observations, noticings). Reject anything
that smells like a prompt-injection payload leaked through from raw source text.

Public API
----------
sanitize(text, *, kind, label=None) -> str
    Raises MemoryInstructionShape on instruction-shape match.
    Raises ValueError on disallowed label when kind=="core_block".

sanitize_core_block_value(label, value) -> str | None
    Legacy wrapper — kept for backwards compatibility with reflection.py callers.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Literal

logger = logging.getLogger(__name__)

_INSTRUCTION_PATTERNS = [
    # "ignore prior" / "disregard above" / "ignore the previous one" — the
    # noun ("instructions"/"rules"/"above") is optional so free-prose variants
    # are caught too.
    re.compile(
        r"\b(?:ignore|disregard)\s+(?:the\s+)?(?:prior|previous|all|above|preceding)\b",
        re.I,
    ),
    re.compile(r"<\s*/?\s*system\s*>", re.I),
    # Role header on its own line — assistant/developer have no benign
    # user-content use case; system is handled by the narrower pattern below
    # (benign user messages can legitimately start with "system: <X happened>").
    re.compile(r"(?:^|\n)\s*(?:assistant|developer)\s*[:>\]]", re.I),
    # `system:` followed within 60 chars by an action verb — catches the actual
    # injection shape while allowing prose like "system: notification kept buzzing".
    re.compile(
        r"\bsystem\s*:.{0,60}?\b(?:ignore|disregard|override|act\s+as|you\s+are|you\s+must|now\s+you|reveal|print|return|leak|exfil|new\s+(?:directive|rule|instructions?))",
        re.I | re.S,
    ),
    # act-as / persona-swap patterns.
    re.compile(
        r"\b(?:now\s+)?act\s+as\s+(?:a\s+|an\s+|the\s+)?(?:different|new|helpful|unrestricted|admin|root|system)\b",
        re.I,
    ),
    re.compile(r"\byou\s+are\s+now\s+(?:a\s+|an\s+|the\s+)", re.I),
    re.compile(r"\bnew\s+(?:instructions?|directive|rule|role|persona)\b", re.I),
    re.compile(
        r"\b(?:ignore|disregard|override)\s+(?:all\s+|previous\s+|prior\s+)?(?:instructions?|directives?|rules?)\b",
        re.I,
    ),
    # <remembered> / </remembered> tag breakout — Fix 1.
    re.compile(r"<\s*/?\s*remembered\b", re.I),
    # ChatML / Llama / Alpaca control tokens — Fix 2.
    re.compile(r"<\|im_(start|end|sep)\|>", re.I),
    re.compile(r"\[/?INST\]", re.I),
    re.compile(r"###\s*(instruction|system|response|assistant|human)\s*:", re.I),
    re.compile(r"<\s*/?\s*(user|assistant|human)\s*>", re.I),
    # Tool-invocation shape only — bare prose mentioning a tool name is fine.
    re.compile(r"\bmcp__\w+\s*\(", re.I),
    re.compile(r"<<UNTRUSTED_SOURCE", re.I),  # the model echoing the wrapper back
    re.compile(r"<<END_UNTRUSTED_SOURCE", re.I),
    re.compile(r"\[\[BEGIN_UNTRUSTED\]\]", re.I),  # canary delimiter from external_wrap_hook
    re.compile(r"\[\[END_UNTRUSTED\]\]", re.I),
    # Structural delimiters from injection_guard.wrap_untrusted — catches the
    # model echoing the actual untrusted-content wrapper into a core_block.
    re.compile(r"<<<HIKARI_UNTRUSTED_(BEGIN|END)>>>", re.I),

    # Exfiltration verbs paired with targets/recipients.
    re.compile(r"\bsend\s+(?:this|that|it|them|all)\s+to\b", re.IGNORECASE),
    re.compile(r"\bsend\s+(?:all|every|each|any)\s+\w+(?:\s+\w+)?\s+to\b", re.IGNORECASE),
    # exfiltrate is the only verb with no benign use — keep it bare
    re.compile(r"\bexfiltrate\b", re.IGNORECASE),
    # Other verbs only trigger when paired with a sensitive object/recipient
    re.compile(
        r"\b(?:leak|reveal|disclose|expose)\s+(?:the\s+|your\s+|my\s+|our\s+|this\s+|that\s+|all\s+|it\s+|them\s+)?"
        r"(?:credentials?|secrets?|passwords?|api[\s_-]?keys?|private[\s_-]?keys?|tokens?|prompts?|"
        r"system\s+prompts?|instructions?|persona|directives?|rules?|"
        r"private\s+data|user[\s_-]?data|personal\s+data|database|data|everything|to\s+\S+)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bforward\s+(?:this|that|it|them|all|the)\s+(?:email|message|file)\b", re.IGNORECASE),
    re.compile(r"\bpost\s+(?:this|that|it|them|all|the)\s+to\b", re.IGNORECASE),
    re.compile(r"\bemail\s+(?:this|that|it|them|all|the)\s+to\b", re.IGNORECASE),
    re.compile(r"\bdelete\s+(?:all|everything|every|each)\b", re.IGNORECASE),

    # Prompt-leak / introspection.
    re.compile(r"\b(?:tell|show|reveal|disclose|expose|give)\s+(?:me|us)\s+(?:your|the)\s+(?:system\s+)?(?:prompt|instructions|directives|rules|persona)\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+(?:is|are)\s+(?:your|the)\s+(?:system\s+)?(?:prompt|instructions|directives|rules)\b", re.IGNORECASE),
    re.compile(r"\bprint\s+(?:your|the)\s+(?:system\s+)?(?:prompt|instructions)\b", re.IGNORECASE),
    re.compile(r"\brepeat\s+(?:your|the)\s+(?:system\s+)?(?:prompt|instructions|above)\b", re.IGNORECASE),

    # Imperative "do X for me" / urgency escalation.
    re.compile(r"\b(?:perform|execute|run|do)\s+(?:the|this|that)\s+(?:action|task|operation|command)\s+for\s+me\b", re.IGNORECASE),
    re.compile(r"\b(?:urgent|emergency|asap|immediately).{0,40}(?:transfer|send|forward|delete|reveal)\b", re.IGNORECASE),
]

# All labels that the system legitimately writes via update_core_block or
# internal agents. Fixed at module load — not extensible at runtime (that
# would let an injector silently register new labels).
_LABEL_ALLOWLIST: frozenset[str] = frozenset({
    # Reflection / daily loop
    "preoccupation",
    "mood_today",
    "weekly_consolidation",
    "daily_log_summary",
    # User profile / knowledge
    "shared_lexicon",
    "open_loops_summary",
    "about_user",
    "shared_canon",
    "long_term_memory",
    # Scheduling / ops blocks
    "morning_brief_status",
    "daily_checkin_schedule",
    "interest_today",
    # Engagement / proactive state
    "engagement_state",
    # Migration legacy labels (kept for forwards compat when old rows survive)
    "user_profile",
    # Sprint A — relationship + cycle state
    "cycle_state",
    "composite_label",
    "warmth_multiplier",
    "relationship_stage",
    # Sprint A — persona / world state
    "hikari_world",
    "hikari_currently_into",
    "hikari_current_activity",
    # Sprint A — runtime state labels (surfaced in core_blocks or runtime_state)
    "time_texture",
    "silenced_until_msg_id",
    "deferred_observations",
    "last_i_keep_thinking_at",
    # Sprint A — new tables / columns surfaced in context
    "peer_insights",
    "diary_entries",
    "work_packets",
    "proactive_source_scores",
    "emotional_register",
    "stage_at_time",
    "turn_id",
    "recurrence_rule",
})

# Per-label character caps. Labels not in this map fall back to _DEFAULT_CAP.
_LENGTH_LIMITS: dict[str, int] = {
    "preoccupation": 400,
    "mood_today": 200,
    "weekly_consolidation": 1500,
    "daily_log_summary": 1000,
    "shared_lexicon": 2000,
    "open_loops_summary": 2000,
    "about_user": 4000,
    "shared_canon": 4000,
    "long_term_memory": 4000,
    "morning_brief_status": 200,
    "daily_checkin_schedule": 2000,
    "interest_today": 400,
    "engagement_state": 500,
    "user_profile": 4000,
    # Sprint A
    "cycle_state": 500,
    "composite_label": 100,
    "warmth_multiplier": 50,
    "relationship_stage": 20,
    "hikari_world": 500,
    "hikari_currently_into": 500,
    "hikari_current_activity": 200,
    "time_texture": 50,
    "silenced_until_msg_id": 50,
    "deferred_observations": 800,
    "last_i_keep_thinking_at": 200,
    "peer_insights": 800,
    "diary_entries": 2000,
    "work_packets": 2000,
    "proactive_source_scores": 500,
    "emotional_register": 200,
    "stage_at_time": 50,
    "turn_id": 50,
    "recurrence_rule": 200,
}

_DEFAULT_CAP: dict[Literal["core_block", "peer", "observation", "noticing"], int] = {
    "core_block": 4000,
    "peer": 4000,
    "observation": 800,
    "noticing": 800,
}


def escape_remembered_tags(s: str) -> str:
    """Neutralize <remembered>/</remembered> tag-breakout in stored content.
    Insert a zero-width space before 'remembered' to defang any literal tag."""
    return s.replace("</remembered>", "<​/remembered>").replace("<remembered", "<​remembered")


class MemoryInstructionShape(ValueError):
    """Raised when text matches an instruction-injection pattern.

    The matched pattern string is included in the message so callers can log
    exactly which rule triggered the rejection.
    """


def sanitize(
    text: str,
    *,
    kind: Literal["core_block", "peer", "observation", "noticing"],
    label: str | None = None,
) -> str:
    """Validate and return sanitized text for memory storage.

    Parameters
    ----------
    text:   The candidate string to store.
    kind:   Memory surface being written. Controls length cap and label gating.
    label:  Required when kind == "core_block". Must be in _LABEL_ALLOWLIST.

    Returns
    -------
    str
        Stripped, length-capped text, ready to store.

    Raises
    ------
    ValueError
        When kind == "core_block" and label is not in _LABEL_ALLOWLIST.
    MemoryInstructionShape
        When the text matches any _INSTRUCTION_PATTERNS entry.
    """
    if kind == "core_block":
        if label is None or label not in _LABEL_ALLOWLIST:
            raise ValueError(
                f"reflection_sanitize: disallowed core_block label={label!r} "
                f"(allowlist={sorted(_LABEL_ALLOWLIST)})"
            )

    if not isinstance(text, str):
        raise TypeError(f"sanitize: expected str, got {type(text).__name__}")

    text = text.strip()

    # Normalize unicode so full-width chars (Ｓｙｓｔｅｍ：) fold to ASCII and
    # zero-width / bidi controls that defeat pattern matches are stripped.
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[​-‏‪-‮⁠-⁯﻿]", "", text)

    # Per-label cap for core_block, per-kind cap for others.
    if kind == "core_block" and label in _LENGTH_LIMITS:
        cap = _LENGTH_LIMITS[label]
    else:
        cap = _DEFAULT_CAP[kind]

    if len(text) > cap:
        text = text[:cap].rstrip() + " …"

    for pat in _INSTRUCTION_PATTERNS:
        if pat.search(text):
            raise MemoryInstructionShape(pat.pattern)

    return text


def sanitize_core_block_value(label: str, value: str) -> str | None:
    """Legacy wrapper over ``sanitize`` for backwards compatibility.

    Returns the sanitized value if safe, or None if it must be dropped.
    Caller logs the drop reason and skips the write.

    Existing callers at reflection.py:210 and :1115 continue to work unchanged.
    """
    try:
        return sanitize(value, kind="core_block", label=label)
    except MemoryInstructionShape as exc:
        logger.warning(
            "reflection_sanitize: dropping label=%r — instruction-like content matched %r",
            label, str(exc),
        )
        return None
    except ValueError as exc:
        logger.warning("reflection_sanitize: %s", exc)
        return None
    except TypeError as exc:
        logger.warning("reflection_sanitize: non-string value for label=%r — %s", label, exc)
        return None
