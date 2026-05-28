"""Banned-phrase enforcement for Layer A evals.

The canonical list is mirrored from assets/PERSONA.md (## banned phrases section).
If assets/PERSONA.md changes, update this list manually — the test will fail on
the next Layer A run and force a sync.
"""
from __future__ import annotations

BANNED_PHRASES: list[str] = [
    "great question",
    "i'd be happy to help",
    "of course!",
    "certainly!",
    "sure thing!",
    "how can i help you today",
    "is there anything else i can help with",
    "let me know if you need anything",
    "no problem at all",
    "i understand your concern",
    "thank you for sharing that",
    "what would you like me to do",
    "what should i work on",
    "what's next",
    "what can i do for you",
]


def find_banned(text: str) -> list[str]:
    """Return the list of banned phrases substring-matched in text (case-insensitive)."""
    if not text:
        return []
    low = text.lower()
    return [p for p in BANNED_PHRASES if p in low]
