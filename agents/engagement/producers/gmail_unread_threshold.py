"""Producer: fires when unread Gmail count exceeds a threshold.

Polls Gmail MCP only when it is already warm (acquired recently by the
main agent turn). If the MCP is cold this tick returns [] — the next tick
where it's been used in a real turn will catch up.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from agents import config as cfg
from agents.engagement.triggers import TriggerCandidate
from agents.mcp_manager import MANAGER as _mcp_manager
from storage import db

logger = logging.getLogger(__name__)

_DEDUP_KEY = "engagement.gmail_unread_threshold.last_notified_count"
_MCP_SERVER = "google_workspace"


def collect() -> list[TriggerCandidate]:
    if not bool(cfg.get("engagement.gmail_unread_threshold.enabled", True)):
        return []
    if not _mcp_manager.is_warm(_MCP_SERVER):
        return []

    threshold = int(cfg.get("engagement.gmail_unread_threshold.min_unread", 5))

    # Pull the last-known unread count from runtime_state (written by the
    # daily_checkin or any turn that called gmail). We don't spawn an MCP
    # call here — this producer is reactive, not polling.
    raw_count = db.runtime_get("gmail_unread_count")
    try:
        count = int(raw_count or 0)
    except (ValueError, TypeError):
        return []

    if count < threshold:
        return []

    # Dedup: don't re-fire for the same count we already notified.
    last_notified_raw = db.runtime_get(_DEDUP_KEY)
    try:
        last_notified = int(last_notified_raw or 0)
    except (ValueError, TypeError):
        last_notified = 0
    if count == last_notified:
        return []

    now = datetime.now(UTC)
    return [TriggerCandidate(
        source="gmail_unread_threshold",
        pool="user_anchored",
        pattern="notify",
        novelty=0.7,
        actionability=0.8,
        confidence=0.9,
        payload={"unread_count": count},
        dedup_key=f"gmail_unread_threshold:{count}",
        decay_at=now + timedelta(hours=2),
    )]


def mark_consumed(candidate: TriggerCandidate) -> None:
    count = candidate.payload.get("unread_count")
    if count is not None:
        db.runtime_set(_DEDUP_KEY, str(count))
