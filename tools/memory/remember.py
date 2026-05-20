"""remember — store one atomic (subject, predicate, object) fact."""
from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from storage import db
from tools import embeddings
from tools._response import ok as _ok


@tool(
    "remember",
    "Store one atomic fact (subject + predicate + object) into Hikari's PRIVATE memory "
    "about the user — preferences, biographical details, things they just told her. "
    "Importance 1-10, confidence 0-1, on_conflict: supersede|coexist|merge. "
    "e.g. user says 'I just got a dog named Mochi' → remember(user, has_pet, 'dog named Mochi'). "
    "Don't use this for a fact-correction with a known prior id (use `mark_fact_invalid`) "
    "or for a follow-up loop without a clock (use `task_create`).",
    {
        "subject": str, "predicate": str, "object": str,
        "importance": int, "confidence": float,
        "on_conflict": str,
    },
)
async def remember(args: dict[str, Any]) -> dict[str, Any]:
    subject = (args.get("subject") or "").strip()
    predicate = (args.get("predicate") or "").strip()
    object_ = (args.get("object") or "").strip()
    if not subject or not predicate or not object_:
        return _ok("remember: subject, predicate, and object are all required.")

    importance = max(1, min(10, int(args.get("importance") or 5)))
    confidence = max(0.0, min(1.0, float(args.get("confidence") or 0.9)))
    on_conflict = (args.get("on_conflict") or "supersede").strip().lower()
    if on_conflict not in ("supersede", "coexist", "merge"):
        on_conflict = "supersede"

    existing = db.active_facts_matching(subject, predicate)

    new_id = db.insert_fact(subject, predicate, object_, importance, confidence)

    try:
        emb = await embeddings.aembed(f"{subject} {predicate} {object_}")
        db.set_vec_fact(new_id, emb)
    except Exception:  # noqa: BLE001
        pass

    superseded: list[int] = []
    if existing and on_conflict == "supersede":
        for old in existing:
            db.supersede_fact(old["id"], new_id, reason="replaced by remember()")
            superseded.append(old["id"])

    msg_parts = [f"stored fact #{new_id}: {subject} {predicate} {object_}"]
    if superseded:
        msg_parts.append(f"superseded {superseded}")
    elif existing and on_conflict == "coexist":
        msg_parts.append(f"coexists with {[e['id'] for e in existing]}")
    return _ok(" — ".join(msg_parts),
               data={"fact_id": new_id, "superseded": superseded})
