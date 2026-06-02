"""Surface completed research summaries as a sideways callback.

Voice: "you said you'd look into X. i did. <summary>."
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from agents.engagement.triggers import TriggerCandidate

logger = logging.getLogger(__name__)


def collect() -> list[TriggerCandidate]:
    from agents import config as cfg
    from storage import db

    if not bool(cfg.get("engagement.research_callback.enabled", True)):
        return []

    # Mood + stage gate.
    stage = db.get_relationship_stage()
    if stage < int(cfg.get("engagement.research_callback.min_stage", 3)):
        return []

    mood = (db.get_core_block("mood_today") or "").lower()
    blocked_moods = ("irritable", "crashed")
    if mood in blocked_moods:
        return []

    with db._conn() as c:
        row = c.execute(
            """
            SELECT id, subject, description, research_summary, research_sources_json
            FROM tasks
            WHERE research_intent = 1
              AND research_summary IS NOT NULL
              AND research_summary != '(no useful sources)'
              AND research_surfaced_at IS NULL
              AND status IN ('open', 'pending')
            ORDER BY created_at ASC
            LIMIT 1
            """
        ).fetchone()

    if not row:
        return []

    return [TriggerCandidate(
        source="research_callback",
        pool="agent_spontaneous",
        pattern="notify",
        payload={
            "task_id": int(row["id"]),
            "subject": str(row["subject"])[:160],
            "summary_excerpt": str(row["research_summary"])[:400],
            "sources_json": row["research_sources_json"] or "[]",
        },
        dedup_key=f"research_callback:{row['id']}",
        decay_at=(datetime.now(UTC) + timedelta(hours=18)).isoformat(),
        novelty=0.9,
        actionability=0.6,
        confidence=0.85,
    )]


def mark_consumed(candidate: TriggerCandidate) -> None:
    from storage import db
    task_id = candidate.payload.get("task_id")
    if not task_id:
        logger.error(
            "research_callback.mark_consumed: task_id missing from payload — marker "
            "NOT written, producer will re-fire. payload=%r", candidate.payload,
        )
        return
    with db._conn() as c:
        c.execute(
            "UPDATE tasks SET research_surfaced_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), int(task_id)),
        )
