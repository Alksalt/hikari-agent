"""Producer: surfaces due reminders as TriggerCandidates.

The actual firing (send_text + mark_fired + repeat-reschedule) remains in
agents/proactive.py:fire_due_reminders — that job stays on its dedicated
60s scheduler so real-time delivery isn't subject to the engagement_tick
pool caps.

This producer only exposes due reminders for the engagement_tick selector
when reminder_fire is in the enabled sources. It does NOT fire them itself;
the scheduler's fire_due_reminders job is the authoritative path.
So this producer exists purely so the selector can weigh a pending reminder
against other candidates and surface it through the unified pipeline if desired.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from agents import config as cfg
from agents.engagement.triggers import TriggerCandidate
from storage import db

logger = logging.getLogger(__name__)


def collect() -> list[TriggerCandidate]:
    if not bool(cfg.get("engagement.reminder_fire.enabled", True)):
        return []
    due = db.reminder_due()
    if not due:
        return []
    now = datetime.now(UTC)
    candidates = []
    for row in due[:3]:  # cap at 3 per tick
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        candidates.append(TriggerCandidate(
            source="reminder_fire",
            pool="user_anchored",
            pattern="notify",
            novelty=0.9,
            actionability=1.0,
            confidence=1.0,
            payload={"text": text, "id": row.get("id"), "fire_at": row.get("fire_at")},
            dedup_key=f"reminder_fire:{row.get('id')}",
            decay_at=now + timedelta(hours=1),
        ))
    return candidates
