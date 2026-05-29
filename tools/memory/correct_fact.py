"""Internal helper used by /memory correct — NOT an MCP-exposed tool."""
from __future__ import annotations

import logging

from storage import db
from tools import embeddings

logger = logging.getLogger(__name__)


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
        fact_category=old.get("fact_category"),
    )
    # Embed for semantic recall — mirror tools/memory/remember.py. Without this
    # the corrected fact (the highest-trust version) had no vector and was
    # invisible to KNN recall. Sync embed (correct_fact is sync); degrade with a
    # warning rather than losing the correction if embedding fails.
    try:
        emb = embeddings.embed(f"{old['subject']} {old['predicate']} {new_object.strip()}")
        db.set_vec_fact(new_id, emb)
    except Exception:
        logger.warning(
            "correct_fact: failed to embed corrected fact %s — semantic recall "
            "degraded for this correction", new_id, exc_info=True,
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
