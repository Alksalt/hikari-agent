"""Guard: rejects generic openers and missing anchor tokens. Returns (ok, reason).
Also exposes should_wake() for the scheduler to short-circuit engagement ticks."""
from __future__ import annotations

import logging
import re

from agents.engagement.triggers import TriggerCandidate

logger = logging.getLogger(__name__)


def should_wake(source_id: str | None = None) -> bool:
    """Return True if the engagement tick should proceed.

    Checks:
    - scheduler_gate_enabled config flag (default True)
    - quiet hours (canonical _is_quiet_now from agents.proactive)
    - global silence window from runtime_state

    Optional per-source min_interval check lives in selector._hard_interval_blocked;
    this gate is the tick-level fast-path that skips the whole producer scan.
    """
    from agents import config as _cfg
    if not bool(_cfg.get("proactive.scheduler_gate_enabled", True)):
        return True  # gate disabled — always wake

    try:
        from agents.proactive_gate import _is_silent_day_today
        if _is_silent_day_today():
            logger.debug("should_wake: silent_day active — skip")
            return False
    except Exception as exc:
        logger.warning("silent_day check failed (fail-open for this gate): %s", exc)
        # fail-open here: if the check itself errors we don't want to permanently
        # suppress ticks — the reserve_and_send gate is still the final authority.

    try:
        from agents.proactive import _is_quiet_now
        if _is_quiet_now():
            logger.debug("should_wake: quiet hours active — skip")
            return False
    except Exception as exc:
        logger.warning("quiet_hours check failed (fail-closed): %s", exc)
        return False  # fail-closed: when in doubt, don't wake the user

    try:
        from datetime import UTC, datetime
        from storage import db as _db
        iso = _db.runtime_get("silence_until")
        if iso:
            until = datetime.fromisoformat(iso)
            if until.tzinfo is None:
                from datetime import timezone
                until = until.replace(tzinfo=UTC)
            if datetime.now(UTC) < until:
                logger.debug("should_wake: global silence active — skip")
                return False
    except Exception as exc:
        logger.warning("quiet_hours check failed (fail-closed): %s", exc)
        return False  # fail-closed: when in doubt, don't wake the user

    return True

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
    # Sprint B Wave 1 — 5 new producers
    "book_just_finished":          ("finished_book",),
    "just_got_home":               (),                        # payload has no stable text anchor; skip check
    "late_night_dissolution":      ("elapsed_hours",),
    "irritation_event":            ("frustration",),
    "weather_mood_shift":          ("to_condition",),
    # Phase H — stale PR producer
    "stale_pr_check":              ("branch", "title"),
    # Phase Q — anniversary callbacks
    "anniversary_callback":        ("summary",),
    # Phase T — belief resurface
    "belief_resurface":            ("statement",),
    # Phase O — background research callback
    "research_callback":           ("subject",),
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
        for path in anchor_paths:
            anchor = candidate.payload.get(path)
            if anchor is not None and str(anchor) in text:
                anchor_found = True
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
