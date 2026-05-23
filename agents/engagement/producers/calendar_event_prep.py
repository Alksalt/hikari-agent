"""Producer: fires when a calendar event starts within the prep lead window.

Reads event data from runtime_state written by existing calendar_heartbeat
logic or main-turn calendar usage. Does not spawn a cold MCP call.
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

_MCP_SERVER = "google_workspace"


def collect() -> list[TriggerCandidate]:
    if not bool(cfg.get("engagement.calendar_event_prep.enabled", True)):
        return []
    if not _mcp_manager.is_warm(_MCP_SERVER):
        return []

    prep_lead = float(cfg.get("calendar_heartbeat.prep_message_lead_minutes", 30))
    jitter = float(cfg.get("calendar_heartbeat.lead_window_jitter_minutes", 5))
    lead_lo = prep_lead - jitter
    lead_hi = prep_lead + jitter

    # Upcoming events written by main-turn calendar usage or daily_checkin.
    raw = db.runtime_get("calendar_upcoming_events")
    if not raw:
        return []
    try:
        events = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(events, list):
        return []

    now = datetime.now(UTC)
    candidates = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        start_iso = ev.get("start_iso") or ev.get("start") or ""
        if not start_iso:
            continue
        try:
            start = datetime.fromisoformat(str(start_iso))
            if start.tzinfo is None:
                start = start.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            continue
        mins_until = (start - now).total_seconds() / 60
        if not (lead_lo <= mins_until <= lead_hi):
            continue
        title = str(ev.get("title") or ev.get("summary") or "(untitled)").strip()
        sig = f"{ev.get('id', '')}|{start_iso}|{title}"
        if db.calendar_notification_exists(sig):
            continue
        candidates.append(TriggerCandidate(
            source="calendar_event_prep",
            pool="user_anchored",
            pattern="notify",
            novelty=0.8,
            actionability=0.9,
            confidence=0.9,
            payload={
                "title": title,
                "summary": title,
                "start_iso": start_iso,
                "minutes_until": int(round(mins_until)),
                "_sig": sig,
            },
            dedup_key=f"calendar_event_prep:{sig}",
            decay_at=start + timedelta(minutes=5),
        ))
        break  # one per tick

    return candidates


def mark_consumed(candidate: TriggerCandidate) -> None:
    sig = candidate.payload.get("_sig")
    if sig:
        db.calendar_notification_set(sig)
