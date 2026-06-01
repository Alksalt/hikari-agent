"""Gmail feature — typed adapter for Google Gmail inbox reads.

Calls ``mcp__google_workspace__query_gmail_emails`` directly via
``agents.mcp_manager.MANAGER``, bypassing the LLM. This is the
fabrication-proof read path used by both ``daily_checkin`` and the
main-turn "check my email" flow.
"""
from __future__ import annotations

from tools.gmail.inbox import query_inbox

ALL_TOOLS = [query_inbox]
