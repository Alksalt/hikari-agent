"""Validate values that reflection wants to write into high-priority memory
surfaces (core_blocks, peer_model). Reject anything that smells like a
prompt-injection payload leaked through from raw source text."""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_INSTRUCTION_PATTERNS = [
    re.compile(r"ignore (?:prior|previous|all|above) instructions?", re.I),
    re.compile(r"<\s*/?\s*system\s*>", re.I),
    re.compile(r"^system\s*:", re.I | re.M),
    re.compile(r"\bmcp__\w+", re.I),
    re.compile(r"<<UNTRUSTED_SOURCE", re.I),  # the model echoing the wrapper back
    re.compile(r"<<END_UNTRUSTED_SOURCE", re.I),
    re.compile(r"\[\[BEGIN_UNTRUSTED\]\]", re.I),  # canary delimiter from external_wrap_hook
    re.compile(r"\[\[END_UNTRUSTED\]\]", re.I),
]

_LABEL_ALLOWLIST = {
    "preoccupation",
    "mood_today",
    "weekly_consolidation",
    "daily_log_summary",
}

_LENGTH_LIMITS = {
    "preoccupation": 400,
    "mood_today": 200,
    "weekly_consolidation": 1500,
    "daily_log_summary": 1000,
}


def sanitize_core_block_value(label: str, value: str) -> str | None:
    """Returns the value if safe, or None if it must be dropped.

    Caller logs the drop reason and skips the write."""
    if label not in _LABEL_ALLOWLIST:
        logger.warning(
            "reflection_sanitize: rejecting unknown label=%r (allowlist=%s)",
            label, sorted(_LABEL_ALLOWLIST),
        )
        return None
    if not isinstance(value, str):
        logger.warning("reflection_sanitize: non-string value for label=%r", label)
        return None
    text = value.strip()
    if not text:
        return None
    limit = _LENGTH_LIMITS.get(label, 500)
    if len(text) > limit:
        text = text[:limit].rstrip() + " …"
    for pat in _INSTRUCTION_PATTERNS:
        if pat.search(text):
            logger.warning(
                "reflection_sanitize: dropping label=%r — instruction-like content matched %r",
                label, pat.pattern,
            )
            return None
    return text
