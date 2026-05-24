"""mark_fact_invalid — flag a stored fact as invalid (optionally superseded)."""
from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from storage import db
from tools._annotations import annotations_for
from tools._response import ok as _ok


@tool(
    "mark_fact_invalid",
    "Invalidate a stored fact by its numeric id (returned earlier by `recall`). "
    "The `fact_id` is the SQLite row id that `recall` surfaces in the `hits[].fact_id` "
    "field — it is now round-tripped through graph payloads so recall always returns it "
    "even for graph-path hits. Pass superseded_by=<new fact id> if there's a replacement, "
    "otherwise the row is just flagged invalid. "
    "e.g. user says 'I never said I hate cilantro, that was sarcastic' "
    "and recall returned fact #42 → mark_fact_invalid(42). "
    "Don't use this when you're inserting a new replacing fact in the same step — "
    "use `remember` with on_conflict='supersede' instead (one call, not two).",
    {"fact_id": int, "reason": str, "superseded_by": int},
    annotations=annotations_for("mark_fact_invalid"),
)
async def mark_fact_invalid(args: dict[str, Any]) -> dict[str, Any]:
    fact_id = int(args.get("fact_id") or 0)
    if not fact_id:
        return _ok("mark_fact_invalid: fact_id is required.")
    reason = (args.get("reason") or "").strip() or None
    raw_sup = args.get("superseded_by")
    superseded_by: int | None
    try:
        superseded_by = int(raw_sup) if raw_sup not in (None, "") else None
    except (TypeError, ValueError):
        superseded_by = None
    db.mark_fact_invalid(fact_id, superseded_by=superseded_by, reason=reason)
    if superseded_by:
        return _ok(
            f"superseded fact #{fact_id} -> #{superseded_by}."
        )
    return _ok(f"invalidated fact #{fact_id}.")
