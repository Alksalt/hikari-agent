"""Producer: fires when a new calendar invitation arrives (opt-in).

Reads invite data from runtime_state written by main-turn calendar usage.
Returns [] when Google Workspace MCP is cold.
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

_DEDUP_KEY = "engagement.calendar_new_invite.notified_ids"
_MCP_SERVER = "google_workspace"


def collect() -> list[TriggerCandidate]:
    if not bool(cfg.get("engagement.calendar_new_invite.enabled", False)):
        return []
    if not _mcp_manager.is_warm(_MCP_SERVER):
        return []

    raw = db.runtime_get("calendar_pending_invites")
    if not raw:
        return []
    try:
        invites = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(invites, list) or not invites:
        return []

    notified_raw = db.runtime_get(_DEDUP_KEY)
    try:
        notified_ids: set[str] = set(json.loads(notified_raw or "[]"))
    except (ValueError, TypeError):
        notified_ids = set()

    candidates = []
    now = datetime.now(UTC)
    for inv in invites:
        if not isinstance(inv, dict):
            continue
        iid = str(inv.get("id") or "")
        if not iid or iid in notified_ids:
            continue
        title = str(inv.get("title") or inv.get("summary") or "(untitled)").strip()
        organizer = str(inv.get("organizer") or "").strip()
        candidates.append(TriggerCandidate(
            source="calendar_new_invite",
            pool="user_anchored",
            pattern="notify",
            novelty=0.75,
            actionability=0.7,
            confidence=0.85,
            payload={"title": title, "summary": title, "organizer": organizer, "id": iid},
            dedup_key=f"calendar_new_invite:{iid}",
            decay_at=now + timedelta(hours=6),
        ))
        break  # one at a time

    return candidates


def mark_consumed(candidate: TriggerCandidate) -> None:
    iid = candidate.payload.get("id")
    if not iid:
        return
    notified_raw = db.runtime_get(_DEDUP_KEY)
    try:
        notified: list[str] = json.loads(notified_raw or "[]")
    except (ValueError, TypeError):
        notified = []
    if iid not in notified:
        notified.append(iid)
    db.runtime_set(_DEDUP_KEY, json.dumps(notified[-50:]))
