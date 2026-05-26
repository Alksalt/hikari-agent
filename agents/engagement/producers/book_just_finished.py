"""Producer: fires when `currently_reading` in hikari_world transitions to a
new book or goes empty — meaning the previous book was just finished.

Transition signal: hikari_world.currently_reading differs from the last-seen
value stored in runtime_state. Fires once per transition, then marks consumed.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from agents import config as cfg
from agents.engagement.triggers import TriggerCandidate
from storage import db

logger = logging.getLogger(__name__)

_LAST_SEEN_KEY = "engagement.book_just_finished.last_seen_currently_reading"


def collect() -> list[TriggerCandidate]:
    if not bool(cfg.get("engagement.book_just_finished.enabled", True)):
        return []

    raw = db.get_core_block("hikari_world")
    if not raw:
        return []
    try:
        world = json.loads(raw)
    except (ValueError, TypeError):
        return []

    currently_reading = (world.get("currently_reading") or "").strip()
    last_seen = (db.runtime_get(_LAST_SEEN_KEY) or "").strip()

    # No book recorded yet at all — nothing to diff.
    if not last_seen and not currently_reading:
        return []

    # First time we see any value: store it, don't fire.
    if not last_seen and currently_reading:
        db.runtime_set(_LAST_SEEN_KEY, currently_reading)
        return []

    # No transition.
    if currently_reading == last_seen:
        return []

    # Transition detected: last_seen was a book title, now it's different
    # (either a new book or empty). The finished book is `last_seen`.
    finished_book = last_seen
    now = datetime.now(UTC)
    return [TriggerCandidate(
        source="book_just_finished",
        pool="agent_spontaneous",
        pattern="notify",
        novelty=0.75,
        actionability=0.5,
        confidence=0.8,
        payload={
            "finished_book": finished_book,
            "now_reading": currently_reading or None,
        },
        dedup_key=f"book_just_finished:{finished_book[:80]}",
        decay_at=now + timedelta(hours=8),
    )]


def mark_consumed(candidate: TriggerCandidate) -> None:
    """Advance the last-seen watermark to the current currently_reading value
    so we don't re-fire on the same transition."""
    raw = db.get_core_block("hikari_world")
    if raw:
        try:
            world = json.loads(raw)
            current = (world.get("currently_reading") or "").strip()
            db.runtime_set(_LAST_SEEN_KEY, current)
            return
        except (ValueError, TypeError):
            pass
    # Fallback: advance to now_reading from the payload (may be empty string).
    now_reading = candidate.payload.get("now_reading") or ""
    db.runtime_set(_LAST_SEEN_KEY, str(now_reading))
