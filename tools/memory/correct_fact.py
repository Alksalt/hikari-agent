"""Internal helper used by /memory correct — NOT an MCP-exposed tool."""
from __future__ import annotations

from storage import db


def correct_fact(old_id: int, new_object: str, *,
                 source_message_id: int | None = None) -> int:
    old = db.fact_by_id(int(old_id))
    if not old:
        raise ValueError(f"correct_fact: unknown fact id {old_id}")
    new_id = db.insert_fact(
        old["subject"], old["predicate"], new_object.strip(),
        importance=old.get("importance", 5),
        confidence=1.0,
        attribution="user_corrected",
        source="user",
        source_message_id=source_message_id,
        source_span_hash=db.span_hash(
            f"{old['subject']} {old['predicate']} {new_object}"
        ),
    )
    db.mark_fact_invalid(int(old_id), superseded_by=new_id,
                         reason="user_corrected via /memory correct")
    with db._conn() as c:
        rows = c.execute(
            "SELECT entity_id FROM fact_entities WHERE fact_id=?",
            (int(old_id),)).fetchall()
    eids = [r["entity_id"] for r in rows]
    if eids:
        db.fact_entities_link(new_id, eids)
    return new_id
