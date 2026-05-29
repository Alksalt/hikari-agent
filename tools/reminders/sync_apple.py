"""Typed adapter for syncing a pending reminder to Apple Reminders.

Calls ``mcp__apple_events__reminders_tasks`` directly via ``MANAGER.call``,
bypassing LLM / prompt plumbing. After a successful create the returned
reminder id is persisted via ``db.reminder_update_apple_event``.

This module is a scheduler-internal caller only — it is NOT registered as an
LLM-reachable @tool. The scheduler imports ``_sync_apple_reminder`` directly
from ``agents/proactive.py``.
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

from agents.mcp_manager import MANAGER, McpCallError
from storage import db

logger = logging.getLogger(__name__)


class AppleReminderResult(BaseModel):
    reminder_id: int
    apple_event_id: str


def _extract_event_id(result: dict[str, Any]) -> str:
    """Pull the Apple Reminders item id out of an MCP call result dict.

    The apple_events MCP server returns JSON in content[0].text or structured
    content. We look for 'id' or 'identifier' fields at the top level or
    inside a nested object.
    """
    # structuredContent path — id at top level
    for key in ("id", "identifier", "reminderIdentifier", "reminder_id"):
        val = result.get(key)
        if val and isinstance(val, str):
            return val.strip()

    # text path — parse JSON
    text = result.get("text") or ""
    if text:
        import json
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            parsed = {}
        if isinstance(parsed, dict):
            for key in ("id", "identifier", "reminderIdentifier", "reminder_id"):
                val = parsed.get(key)
                if val and isinstance(val, str):
                    return val.strip()

    return ""


async def _sync_apple_reminder(
    reminder_id: int,
    title: str,
    due_iso: str,
    list_name: str = "Reminders",
) -> AppleReminderResult:
    """Create or update an Apple Reminders item for the given reminder.

    If ``db.reminder_get`` reports an existing ``apple_event_id`` the stale
    item is deleted before creating a fresh one (the apple_events MCP exposes
    no update endpoint). Failures on the delete step are logged but not fatal.

    Args match what the proactive.py path was sending: title (verbatim),
    dueDate (ISO string), list name.

    Raises ``McpCallError`` on tool error.
    """
    # Delete the stale item when rescheduling (snooze) — avoids orphaned
    # duplicates. Failures are logged but not fatal so the create still runs.
    existing_row = db.reminder_get(reminder_id)
    existing_apple_id = (existing_row or {}).get("apple_event_id") or ""
    if existing_apple_id:
        try:
            await MANAGER.call(
                "apple_events",
                "delete_reminder",
                {"id": existing_apple_id},
            )
        except Exception:
            logger.warning(
                "sync_apple: could not delete stale reminder %s for reminder %d — continuing",
                existing_apple_id,
                reminder_id,
            )

    result = await MANAGER.call(
        "apple_events",
        "create_reminder",
        {
            "title": title,
            "dueDate": due_iso,
            "listName": list_name,
        },
    )
    apple_event_id = _extract_event_id(result)
    if not apple_event_id:
        raise McpCallError(
            "apple_events",
            "create_reminder",
            f"no event_id in result: {result!r}",
        )
    db.reminder_update_apple_event(reminder_id, apple_event_id)
    return AppleReminderResult(reminder_id=reminder_id, apple_event_id=apple_event_id)


# NOTE: sync_apple_reminder is a scheduler-internal caller.
# It is intentionally NOT registered as an LLM-reachable @tool.
# Use _sync_apple_reminder() directly from agents/proactive.py.
