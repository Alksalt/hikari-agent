"""Producer: re-engagement nudge when user has been silent 2-6h (opt-in).

Extracts logic from agents/proactive.py:should_send_reengagement so the
unified engagement_tick can weigh it against other candidates.
Phase J: maybe_send_reengagement was deleted; engagement_tick is now the
sole driver for reengage nudges.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

from agents import config as cfg
from agents.engagement.triggers import TriggerCandidate
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


def _is_quiet_now() -> bool:
    from agents import config as _cfg
    from agents.hooks import _resolve_local_tz_name
    p = _cfg.section("proactive")
    start = dtime(int(p.get("quiet_start_hour", 23)), 0)
    end = dtime(int(p.get("quiet_end_hour", 8)), 0)
    tz = ZoneInfo(_resolve_local_tz_name())
    now = datetime.now(tz).time()
    if start <= end:
        return start <= now < end
    return now >= start or now < end


def collect() -> list[TriggerCandidate]:
    if not bool(cfg.get("engagement.reengage_silence.enabled", False)):
        return []
    if _is_quiet_now():
        return []

    now = datetime.now(UTC)
    silence_until = _parse_dt(db.runtime_get("silence_until"))
    if silence_until and now < silence_until:
        return []

    rows = db.recent_messages(limit=1)
    if not rows:
        return []
    last = rows[0]
    if last["role"] != "assistant":
        return []
    last_ts = _parse_dt(last["ts"])
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
