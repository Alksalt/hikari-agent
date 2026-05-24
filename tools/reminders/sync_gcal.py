"""Typed adapter for syncing a pending reminder to Google Calendar.

Calls ``mcp__google_workspace__create_calendar_event`` directly via
``MANAGER.call``, bypassing LLM / prompt plumbing. After a successful
create the returned event_id is persisted via ``db.reminder_update_gcal_event``.

This module is a scheduler-internal caller only — it is NOT registered as an
LLM-reachable @tool. The scheduler imports ``_sync_gcal_reminder`` directly
from ``agents/proactive.py``.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from agents.mcp_manager import MANAGER, McpCallError
from storage import db

logger = logging.getLogger(__name__)


class GCalReminderResult(BaseModel):
    reminder_id: int
    gcal_event_id: str


def _extract_event_id(result: dict[str, Any]) -> str:
    """Pull the Google Calendar event id out of an MCP call result dict."""
    import json

    # structuredContent path
    for key in ("id", "event_id", "eventId"):
        val = result.get(key)
        if val and isinstance(val, str):
            return val.strip()

    # text path
    text = result.get("text") or ""
    if text:
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            parsed = {}
        if isinstance(parsed, dict):
            for key in ("id", "event_id", "eventId"):
                val = parsed.get(key)
                if val and isinstance(val, str):
                    return val.strip()

    return ""


def _compute_end_time(start_iso: str) -> str:
    """Return start_iso + 30 minutes as an ISO string.

    Falls back to start_iso + "Z" on parse failure to avoid a crash.
    """
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M%z",
        "%Y-%m-%dT%H:%M",
    ):
        try:
            dt = datetime.strptime(start_iso, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return (dt + timedelta(minutes=30)).isoformat()
        except ValueError:
            continue
    return start_iso


async def _sync_gcal_reminder(
    reminder_id: int,
    title: str,
    start_iso: str,
    calendar_id: str = "primary",
) -> GCalReminderResult:
    """Call google_workspace/create_calendar_event and parse the result.

    Args mirror what the proactive.py prompt was requesting: title (verbatim),
    start_time, end_time (start + 30 min), description, calendar_id.

    Raises ``McpCallError`` on tool error.
    """
    end_time = _compute_end_time(start_iso)
    result = await MANAGER.call(
        "google_workspace",
        "create_calendar_event",
        {
            "summary": title,
            "start_time": start_iso,
            "end_time": end_time,
            "description": f"hikari reminder #{reminder_id}",
            "calendar_id": calendar_id,
        },
    )
    gcal_event_id = _extract_event_id(result)
    if not gcal_event_id:
        raise McpCallError(
            "google_workspace",
            "create_calendar_event",
            f"no event_id in result: {result!r}",
        )
    db.reminder_update_gcal_event(reminder_id, gcal_event_id)
    return GCalReminderResult(reminder_id=reminder_id, gcal_event_id=gcal_event_id)


# NOTE: sync_gcal_reminder is a scheduler-internal caller.
# It is intentionally NOT registered as an LLM-reachable @tool.
# Use _sync_gcal_reminder() directly from agents/proactive.py.
