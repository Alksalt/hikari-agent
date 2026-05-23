"""Producer: fires when a new file is added to Google Drive Starred (opt-in).

Reads from runtime_state written by main-turn Drive usage. Returns []
when Google Workspace MCP is cold.
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

_DEDUP_KEY = "engagement.drive_starred_new.last_seen_ids"
_MCP_SERVER = "google_workspace"


def collect() -> list[TriggerCandidate]:
    if not bool(cfg.get("engagement.drive_starred_new.enabled", False)):
        return []
    if not _mcp_manager.is_warm(_MCP_SERVER):
        return []

    raw = db.runtime_get("drive_starred_files")
    if not raw:
        return []
    try:
        files = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(files, list) or not files:
        return []

    seen_raw = db.runtime_get(_DEDUP_KEY)
    try:
        seen_ids: set[str] = set(json.loads(seen_raw or "[]"))
    except (ValueError, TypeError):
        seen_ids = set()

    candidates = []
    now = datetime.now(UTC)
    for f in files:
        if not isinstance(f, dict):
            continue
        fid = str(f.get("id") or "")
        if not fid or fid in seen_ids:
            continue
        name = str(f.get("name") or f.get("title") or "(unnamed)").strip()
        candidates.append(TriggerCandidate(
            source="drive_starred_new",
            pool="user_anchored",
            pattern="notify",
            novelty=0.7,
            actionability=0.6,
            confidence=0.8,
            payload={"name": name, "id": fid},
            dedup_key=f"drive_starred_new:{fid}",
            decay_at=now + timedelta(hours=12),
        ))
        break  # one at a time

    return candidates


def mark_consumed(candidate: TriggerCandidate) -> None:
    fid = candidate.payload.get("id")
    if not fid:
        return
    seen_raw = db.runtime_get(_DEDUP_KEY)
    try:
        seen: list[str] = json.loads(seen_raw or "[]")
    except (ValueError, TypeError):
        seen = []
    if fid not in seen:
        seen.append(fid)
    db.runtime_set(_DEDUP_KEY, json.dumps(seen[-50:]))
