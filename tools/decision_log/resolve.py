from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from storage import db
from tools._response import ok as _ok

TOOL_NAME = "decision_log_resolve"


@tool(
    TOOL_NAME,
    "Record the outcome of a previously-captured prediction. Call when the "
    "user answers a calibration check with a clear yes/no. Immutable — "
    "same outcome re-applied is a no-op; a DIFFERENT outcome is refused "
    "to protect the calibration ledger from prompt-injected revisions.",
    {"decision_id": int, "outcome": int},
)
async def decision_log_resolve(args: dict[str, Any]) -> dict[str, Any]:
    try:
        did = int(args.get("decision_id"))
        outcome = int(args.get("outcome"))
    except (TypeError, ValueError):
        return _ok("decision_log_resolve: decision_id and outcome must be ints.")
    if outcome not in (0, 1):
        return _ok(f"decision_log_resolve: outcome must be 0 or 1; got {outcome}.")
    try:
        db.decision_resolve(did, outcome)
    except ValueError as exc:
        return _ok(f"decision_log_resolve: {exc}")
    except Exception as exc:
        return _ok(f"decision_log_resolve: failed: {exc!r}")
    label = "happened" if outcome == 1 else "didn't"
    return _ok(f"decision #{did} resolved: {label}.")


ALL_TOOLS = [decision_log_resolve]
