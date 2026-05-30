"""Producer: re-engagement nudge when user has been silent 2-6h (opt-in).

Extracts logic from agents/proactive.py:should_send_reengagement so the
unified engagement_tick can weigh it against other candidates.
Phase J: maybe_send_reengagement was deleted; engagement_tick is now the
sole driver for reengage nudges.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from agents import config as cfg
from agents.engagement.triggers import TriggerCandidate
from agents.proactive import _is_quiet_now
from storage import db

logger = logging.getLogger(__name__)


def _parse_dt(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(iso)
        if d.tzinfo is None:
            d = d.replace(tzinfo=UTC)
        return d
    except (ValueError, TypeError):
        return None


def collect() -> list[TriggerCandidate]:
    if not bool(cfg.get("engagement.reengage_silence.enabled", False)):
        return []
    if _is_quiet_now():
        return []

    # Stage gate: don't fire before the relationship has matured enough.
    stage = db.runtime_get_int("relationship_stage", 1)
    min_stage = int(cfg.get("engagement.reengage_silence.min_stage", 6))
    if stage < min_stage:
        logger.debug("reengage_silence: stage %d < min_stage %d — skipping", stage, min_stage)
        return []

    # Hard interval gate — same check used by the selector for all other sources.
    # Prevents reengage_silence from bypassing the min_interval_minutes config.
    from agents.engagement.selector import _hard_interval_blocked
    from storage import db as _db_for_last_send
    last_send_iso = _db_for_last_send.runtime_get("last_send_reengage_silence")
    if _hard_interval_blocked("reengage_silence", {"reengage_silence": last_send_iso} if last_send_iso else {}):
        logger.debug("reengage_silence: _hard_interval_blocked — skipping")
        return []

    now = datetime.now(UTC)

    # Anchor the silence gap on the last inbound user message — proactive
    # assistant rows now persist (Phase 4A) and would otherwise reset the
    # anchor every time we fire, breaking dedup.
    last_user_ts_iso = db.runtime_get("last_user_message")
    if not last_user_ts_iso:
        return []
    last_ts = _parse_dt(last_user_ts_iso)
    if not last_ts:
        return []

    p = cfg.section("proactive")
    lo = float(p.get("reengage_min_hours", 2))
    hi = float(p.get("reengage_max_hours", 6))
    elapsed = (now - last_ts).total_seconds() / 3600
    if not (lo <= elapsed <= hi):
        return []

    sent_for = db.runtime_get("reengage_sent_for_gap")
    if sent_for == last_ts.isoformat():
        return []

    return [TriggerCandidate(
        source="reengage_silence",
        pool="agent_spontaneous",
        pattern="notify",
        novelty=0.5,
        actionability=0.6,
        confidence=0.8,
        payload={"elapsed_hours": round(elapsed, 1), "last_message_ts": last_ts.isoformat()},
        dedup_key=f"reengage_silence:{last_ts.isoformat()}",
        decay_at=now + timedelta(hours=4),
    )]


def mark_consumed(candidate: TriggerCandidate) -> None:
    last_ts = candidate.payload.get("last_message_ts")
    if last_ts:
        db.runtime_set("reengage_sent_for_gap", str(last_ts))
    # Record send time so _hard_interval_blocked can gate subsequent ticks.
    db.runtime_set("last_send_reengage_silence", datetime.now(UTC).isoformat())
