"""Producer: Readwise daily review (stubbed — MCP removed 2026-05-21).

Readwise MCP was removed from .mcp.json on 2026-05-21 per log entry. This
producer always returns [] until Readwise is migrated to a hosted HTTP MCP
and re-added. Do NOT re-add the Readwise MCP server.
"""
from __future__ import annotations

from agents.engagement.triggers import TriggerCandidate


def collect() -> list[TriggerCandidate]:
    return []
