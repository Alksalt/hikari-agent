"""Calendar feature — typed adapter for Google Calendar events.

Calls ``mcp__google_workspace__calendar_get_events`` directly via
``agents.mcp_manager.MANAGER``, bypassing the LLM.
"""
from __future__ import annotations

from tools.calendar.get_events import calendar_get_events

ALL_TOOLS = [calendar_get_events]
