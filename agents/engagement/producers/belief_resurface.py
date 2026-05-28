"""Belief resurface producer — surfaces matured belief_journal entries.

Voice: "three months ago you said you'd stop working weekends. ...was that accurate."
Stage 3+ only. Sunday ceremony preference, but can fire any day.
"""
from __future__ import annotations

import logging
from datetime import datetime, UTC

from agents.engagement.triggers import TriggerCandidate

logger = logging.getLogger(__name__)


def collect() -> list[TriggerCandidate]:
    from agents import config as cfg
    from storage import db

    if not bool(cfg.get("engagement.belief_resurface.enabled", True)):
        return []

    min_stage = int(cfg.get("engagement.belief_resurface.min_stage", 3))
    stage = db.runtime_get_int("relationship_stage", 1)
    if stage < min_stage:
        return []

    # Per-session cap.
    last_session = db.runtime_get("belief_resurface_last_session_id")
    current_session = db.get_session_id() or ""
    if last_session and last_session == current_session:
        return []

    due = db.belief_journal_due()
    if not due:
        return []

    # Pick oldest.
    item = due[0]

    return [TriggerCandidate(
        source="belief_resurface",
        pool="agent_spontaneous",
        pattern="notify",
        payload={
            "belief_id": item["id"],
            "statement": item["statement"],
            "claim_type": item["claim_type"],
            "stated_at": item["stated_at"],
        },
        dedup_key=f"belief_resurface:{item['id']}",
        decay_at=datetime.now(UTC).replace(microsecond=0).isoformat(),  # 24h decay
        novelty=0.8,
        actionability=0.3,
        confidence=0.9,
    )]


def mark_consumed(belief_id: int | None = None) -> None:
    from storage import db
    sid = db.get_session_id() or ""
    if sid:
        db.runtime_set("belief_resurface_last_session_id", sid)
    if belief_id:
        db.belief_journal_resolve(int(belief_id), note="surfaced")
