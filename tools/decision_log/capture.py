"""decision_log_capture — MCP tool Hikari calls when she catches a
prediction speech act from the user. Stores one row in the decisions table.

CLAUDE.md teaches the trigger phrases; this tool is the writer. Returns a
short in-voice ack so Hikari can move on without ceremony.
"""
from __future__ import annotations

from claude_agent_sdk import tool

from storage import db
from tools._response import ok as _ok
from tools.decision_log._shared import TOOL_NAME


@tool(
    TOOL_NAME,
    "Log a user's prediction so we can score calibration later. Use when "
    "the user states a probability + a date ('i think we ship friday at "
    "80%', 'probably 60% chance the deal closes by next monday').",
    {
        "statement": str,
        "predicted_p": float,
        "resolve_by": str,
        "reasoning": str,
    },
)
async def decision_log_capture(args: dict) -> dict:
    """args: statement (str), predicted_p (float in [0,1]), resolve_by (ISO
    date YYYY-MM-DD), reasoning (optional str). Returns OK with the row id."""
    statement = str(args.get("statement") or "").strip()
    if not statement:
        return _ok("decision_log_capture: statement is required.")
    try:
        p = float(args.get("predicted_p") or 0.0)
    except (TypeError, ValueError):
        return _ok("decision_log_capture: predicted_p must be a number.")
    if not (0.0 <= p <= 1.0):
        return _ok(f"decision_log_capture: predicted_p must be in [0,1]; got {p}.")
    resolve_by = str(args.get("resolve_by") or "").strip()
    if not resolve_by:
        return _ok("decision_log_capture: resolve_by is required.")
    reasoning = str(args.get("reasoning") or "").strip() or None

    did = db.decision_insert(statement, p, resolve_by, reasoning)
    return _ok(f"logged decision #{did} at p={p}, resolve {resolve_by}.")


ALL_TOOLS = [decision_log_capture]
