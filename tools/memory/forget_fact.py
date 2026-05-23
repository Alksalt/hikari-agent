"""Internal helper used by /memory forget — NOT an MCP-exposed tool."""
from __future__ import annotations

from storage import db


def forget_fact(fact_id: int, *, reason: str = "user_forget") -> bool:
    """Mark a fact invalid. Returns True if the fact existed and was invalidated."""
    fid = int(fact_id)
    if not db.fact_by_id(fid):
        return False
    db.mark_fact_invalid(fid, superseded_by=None, reason=reason)
    return True
