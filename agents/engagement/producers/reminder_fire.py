"""Producer: surfaces due reminders as SILENT TriggerCandidates.

NO-SEND CONTRACT — the actual firing (send_text + mark_fired +
repeat-reschedule + keyboards + the proactive-disabled exemption) lives in
agents/proactive.py:fire_due_reminders on its dedicated 60s job. This
producer is registered with ``send_mode: silent`` in config/engagement.yaml,
so the selector filters it before any send; its only purpose is awareness —
the selector's suppression window holds back competing proactive pings when
a reminder is about to land. The dedup_key deliberately shares the owner's
``reminder:`` namespace, and this module must never define mark_consumed or
mark_fired (tests/test_reminder_fire_no_double.py pins both).
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
            dedup_key=f"reminder:{row.get('id')}",
            decay_at=now + timedelta(hours=1),
        ))
    return candidates
