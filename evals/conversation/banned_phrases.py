"""Banned-phrase enforcement for Layer A evals.

The canonical list is mirrored from assets/PERSONA.md (## banned phrases section).
If assets/PERSONA.md changes, update this list manually — the test will fail on
the next Layer A run and force a sync.

Entries are either plain strings (substring match, case-insensitive) or compiled
re.Pattern objects (regex match, case-insensitive). The task-tail entries below are
end-anchored regexes to avoid false positives in mid-sentence uses — e.g.
"so what's next on the agenda" must NOT match, but "all done. what's next?" must.
"""
from __future__ import annotations

import re

_TASK_TAIL = re.compile(
    r"(what'?s next|what should i work on|what can i do for you)\??\s*$",
    re.IGNORECASE,
)

BANNED_PHRASES: list[str | re.Pattern] = [
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
    # Task-tail entries: end-anchored so they only fire at the end of a message.
    _TASK_TAIL,
]


def find_banned(text: str) -> list[str]:
    """Return the list of banned phrases matched in text (case-insensitive).

    Plain-string entries are matched as case-insensitive substrings.
    Compiled re.Pattern entries are matched with .search().
    """
    if not text:
        return []
    low = text.lower()
    hits: list[str] = []
    for p in BANNED_PHRASES:
        if isinstance(p, re.Pattern):
            if p.search(text):
                hits.append(p.pattern)
        else:
            if p in low:
                hits.append(p)
    return hits
