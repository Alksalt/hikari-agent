"""Guard: rejects generic openers and missing anchor tokens. Returns (ok, reason)."""
from __future__ import annotations

import re

from agents.engagement.triggers import TriggerCandidate

_GENERIC_OPENER = re.compile(
    r"^(hey|hi|just checking|how are you|what'?s up|good morning,?\s*$)",
    re.IGNORECASE,
)


def passes(text: str, candidate: TriggerCandidate) -> tuple[bool, str]:
    if not text:
        return False, "empty"
    if _GENERIC_OPENER.search(text.strip()):
        return False, "generic_opener"
    if candidate.source == "wiki_new_file":
        anchor = candidate.payload.get("filename") or ""
        if anchor and anchor not in text:
            return False, f"missing_anchor:{anchor!r}"
        if candidate.pattern == "question" and not text.rstrip().endswith(("?", "y/n.", "no.")):
            return False, "question_pattern_missing_question_mark"
    return True, "ok"
