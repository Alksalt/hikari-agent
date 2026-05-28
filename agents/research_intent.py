"""Detect when a user message implies pending research Hikari should do.

Patterns: "i'll look into X", "let me think about Y", "i wonder if Z",
"i need to research X", "remind me to investigate Y". Excludes false
positives like "i'll look into your eyes".
"""
from __future__ import annotations

import re


RESEARCH_CUE_RE = re.compile(
    r"(?i)\b(i'?ll (think about|look into|read up on|research|check)|"
    r"let me (look into|think about|read about)|"
    r"i (want|need) to (research|find out|investigate|look into)|"
    r"i wonder (if|whether|how|why|what)|"
    r"(remind me to|i should) (research|investigate|look into|read about))\b"
)
EXCLUSION_RE = re.compile(
    r"(?i)\b(look into (your|his|her|my) (eyes|face|soul)|"
    r"think about (you|us|him|her|me))\b"
)


def is_research_intent(text: str) -> tuple[bool, str | None]:
    """Returns (True, fragment) if the text contains a research intent cue."""
    if not text:
        return False, None
    if EXCLUSION_RE.search(text):
        return False, None
    m = RESEARCH_CUE_RE.search(text)
    if not m:
        return False, None
    fragment = m.group(0)
    if len(fragment) < 8:
        return False, None
    return True, fragment
