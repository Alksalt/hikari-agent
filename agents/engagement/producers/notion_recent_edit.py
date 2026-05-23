"""Producer: fires when a Notion page was recently edited (opt-in).

Reads from runtime_state written by main-turn Notion usage. Returns []
when Notion MCP is cold.
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

_DEDUP_KEY = "engagement.notion_recent_edit.notified_page_ids"
_MCP_SERVER = "notion"


def collect() -> list[TriggerCandidate]:
    if not bool(cfg.get("engagement.notion_recent_edit.enabled", False)):
        return []
    if not _mcp_manager.is_warm(_MCP_SERVER):
        return []

    raw = db.runtime_get("notion_recent_edits")
    if not raw:
        return []
    try:
        pages = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(pages, list) or not pages:
        return []

    notified_raw = db.runtime_get(_DEDUP_KEY)
    try:
        notified_ids: set[str] = set(json.loads(notified_raw or "[]"))
    except (ValueError, TypeError):
        notified_ids = set()

    candidates = []
    now = datetime.now(UTC)
    for p in pages:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or "")
        if not pid or pid in notified_ids:
            continue
        page_title = str(p.get("title") or p.get("name") or "(untitled)").strip()
        candidates.append(TriggerCandidate(
            source="notion_recent_edit",
            pool="user_anchored",
            pattern="notify",
            novelty=0.65,
            actionability=0.55,
            confidence=0.8,
            payload={"page_title": page_title, "id": pid},
            dedup_key=f"notion_recent_edit:{pid}",
            decay_at=now + timedelta(hours=8),
        ))
        break  # one at a time

    return candidates


def mark_consumed(candidate: TriggerCandidate) -> None:
    pid = candidate.payload.get("id")
    if not pid:
        return
    notified_raw = db.runtime_get(_DEDUP_KEY)
    try:
        notified: list[str] = json.loads(notified_raw or "[]")
    except (ValueError, TypeError):
        notified = []
    if pid not in notified:
        notified.append(pid)
    db.runtime_set(_DEDUP_KEY, json.dumps(notified[-50:]))
