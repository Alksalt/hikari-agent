"""Guard: rejects generic openers and missing anchor tokens. Returns (ok, reason)."""
from __future__ import annotations

import re

from agents.engagement.triggers import TriggerCandidate

_GENERIC_OPENER = re.compile(
    r"^(hey|hi|just checking|how are you|what'?s up|good morning,?\s*$)",
    re.IGNORECASE,
)

# Anchor token paths per source. Each value is a tuple of payload keys;
# the guard checks that at least one matching value appears verbatim in the
# composed text. Sources with an empty tuple skip the anchor check.
ANCHOR_TOKEN_PATHS: dict[str, tuple[str, ...]] = {
    "gmail_unread_threshold":      ("unread_count",),
    "calendar_event_prep":         ("title", "summary"),
    "calendar_new_invite":         ("title", "summary"),
    "wiki_new_file":               ("filename",),
    "reminder_fire":               ("text",),
    "decision_resolve_due":        ("statement",),
    "callback_episode":            ("text",),
    "drive_starred_new":           ("name",),
    "notion_recent_edit":          ("page_title",),
    "weather_alert":               ("alert_summary",),
    "weirdly_good_mood_leak":      (),
    "reengage_silence":            (),
    "location_arrived_recurring":  ("place_name",),
    "readwise_daily_review":       ("highlight_count",),
    "gmail_important_thread":      ("subject",),
}


def passes(text: str, candidate: TriggerCandidate) -> tuple[bool, str]:
    """Return (True, 'ok') if the composed text passes all checks, else
    (False, reason) where reason is a short slug for logging."""
    if not text:
        return False, "empty"
    if _GENERIC_OPENER.search(text.strip()):
        return False, "generic_opener"

    anchor_paths = ANCHOR_TOKEN_PATHS.get(candidate.source, ())

    if anchor_paths:
        # Find the first anchor value that appears verbatim in the text.
        anchor_found: bool = False
        found_anchor_value: str | None = None
        for path in anchor_paths:
            anchor = candidate.payload.get(path)
            if anchor is not None and str(anchor) in text:
                anchor_found = True
                found_anchor_value = str(anchor)
                break
        if not anchor_found:
            # Include one representative value in the reason for debugging.
            first_val = None
            for path in anchor_paths:
                v = candidate.payload.get(path)
                if v is not None:
                    first_val = str(v)
                    break
            hint = f":{first_val!r}" if first_val else f":{anchor_paths!r}"
            return False, f"missing_anchor{hint}"

    # Question-pattern check applies whether or not there are anchors.
    if candidate.pattern == "question" and not text.rstrip().endswith(("?", "y/n.", "no.")):
        return False, "question_pattern_missing_question_mark"

    return True, "ok"
