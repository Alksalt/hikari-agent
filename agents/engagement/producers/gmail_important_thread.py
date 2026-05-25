"""Producer: fires when a high-priority / urgent-label email thread exists.

Reads from runtime_state key written by daily_checkin or main-turn Gmail
usage — does not spawn a cold MCP call. Returns [] when MCP is cold.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from agents import config as cfg
from agents.engagement.triggers import TriggerCandidate
from agents.mcp_manager import MANAGER as _mcp_manager
from storage import db

logger = logging.getLogger(__name__)

_DEDUP_KEY = "engagement.gmail_important_thread.last_notified_ids"
_MCP_SERVER = "google_workspace"


def collect() -> list[TriggerCandidate]:
    if not bool(cfg.get("engagement.gmail_important_thread.enabled", False)):
        return []
    if not _mcp_manager.is_warm(_MCP_SERVER):
        return []

    # Important threads written by daily_checkin into runtime_state as JSON list:
    # [{"id": "...", "subject": "...", "sender": "..."}, ...]
    raw = db.runtime_get("gmail_important_threads")
    if not raw:
        return []
    try:
        threads = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(threads, list) or not threads:
        return []

    # Dedup: skip threads we already notified.
    notified_raw = db.runtime_get(_DEDUP_KEY)
    try:
        notified_ids: set[str] = set(json.loads(notified_raw or "[]"))
    except (ValueError, TypeError):
        notified_ids = set()

    candidates = []
    now = datetime.now(UTC)
    for t in threads:
        tid = str(t.get("id") or "")
        if not tid or tid in notified_ids:
            continue
        subject = str(t.get("subject") or "(no subject)").strip()
        sender = str(t.get("sender") or "").strip()
        candidates.append(TriggerCandidate(
            source="gmail_important_thread",
            pool="user_anchored",
            pattern="notify",
            novelty=0.85,
            actionability=0.9,
            confidence=0.85,
            payload={"subject": subject, "sender": sender, "id": tid},
            dedup_key=f"gmail_important_thread:{tid}",
            decay_at=now + timedelta(hours=4),
        ))
        break  # one at a time

    return candidates


def mark_consumed(candidate: TriggerCandidate) -> None:
    tid = candidate.payload.get("id")
    if not tid:
        return
    notified_raw = db.runtime_get(_DEDUP_KEY)
    try:
        notified: list[str] = json.loads(notified_raw or "[]")
    except (ValueError, TypeError):
        notified = []
    if tid not in notified:
        notified.append(tid)
    # Keep last 50 to bound size.
    db.runtime_set(_DEDUP_KEY, json.dumps(notified[-50:]))
