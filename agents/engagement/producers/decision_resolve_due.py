"""Producer: surfaces overdue unresolved decisions as TriggerCandidates.

Logic extracted from agents/decision_log.py:run_decision_resolver.
The Sunday-cron resolver remains in place; this producer exposes overdue
decisions for the unified engagement_tick selector as well.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from agents import config as cfg
from agents.engagement.triggers import TriggerCandidate
from storage import db

logger = logging.getLogger(__name__)


def collect() -> list[TriggerCandidate]:
    if not bool(cfg.get("engagement.decision_resolve_due.enabled", True)):
        return []
    cooldown = int(cfg.get("decision_log.reask_cooldown_days", 14))
    due = db.decisions_unresolved_due(limit=1, cooldown_days=cooldown)
    if not due:
        return []
    now = datetime.now(UTC)
    row = due[0]
    statement = str(row.get("statement") or "").strip()
    if not statement:
        return []
    predicted_p = row.get("predicted_p")
    resolve_by = str(row.get("resolve_by") or "")
    return [TriggerCandidate(
        source="decision_resolve_due",
        pool="user_anchored",
        pattern="question",
        novelty=0.7,
        actionability=0.85,
        confidence=0.9,
        payload={
            "statement": statement,
            "predicted_p": predicted_p,
            "resolve_by": resolve_by,
            "id": row.get("id"),
        },
        dedup_key=f"decision_resolve_due:{row.get('id')}",
        decay_at=now + timedelta(hours=24),
    )]
