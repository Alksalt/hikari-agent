"""Memory tools exposed to the agent via in-process MCP server.

These wrap storage.db with input validation and return shapes the LLM finds easy
to read. Returned dicts always have a top-level 'content' list of MCP-style
text blocks plus a 'data' field for structured payloads.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from storage import db, retrieval
from tools import embeddings


def _ok(text: str, data: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if data is not None:
        body["data"] = data
    return body


@tool(
    "recall",
    "Search Hikari's memory (facts + episodes) for things relevant to the given query. "
    "Returns top-N hits ranked by recency, importance, and BM25 relevance, plus a "
    "confidence score in [0, 1] derived from the top hit's relevance. If confidence is "
    "below the calibration threshold (see config.recall_calibration), say so to the lead "
    "rather than padding with low-quality matches. "
    "Use this whenever the user references past events, asks 'remember when', or you "
    "need context to give a real answer instead of a generic one.",
    {"query": str, "limit": int},
)
async def recall(args: dict[str, Any]) -> dict[str, Any]:
    from agents import config as cfg
    query = (args.get("query") or "").strip()
    limit = int(args.get("limit") or 8)
    if not query:
        return _ok(
            "recall: empty query, no results.",
            data={"confidence": 0.0, "hits": []},
        )
    hits = retrieval.retrieve(query, limit=limit)
    threshold = float(cfg.get("recall_calibration.confidence_threshold", 0.6))
    if not hits:
        return _ok(
            f"recall: no memory matches for {query!r}. confidence: 0.00 (low). "
            f"recommend telling the lead you don't remember.",
            data={"confidence": 0.0, "below_threshold": True, "hits": []},
        )
    top = hits[0]
    # Cold-start guard: relevance is normalized within the candidate pool, so a
    # single off-topic hit can self-certify as relevance=1.0. Scale by hit count
    # so confidence on a tiny pool isn't artificially high. Cap floor at 0.3
    # contribution from hit count alone.
    hit_factor = min(1.0, len(hits) / 3.0)
    confidence = float(top.relevance) * hit_factor
    below = confidence < threshold
    bucket = "low" if confidence < 0.4 else "medium" if confidence < 0.7 else "high"
    header = (
        f"confidence: {confidence:.2f} ({bucket}"
        f"{'; BELOW THRESHOLD' if below else ''}). "
        f"top {len(hits)} matches for {query!r}:"
    )
    lines = [header]
    for h in hits:
        lines.append(
            f"  [{h.kind}#{h.ref_id} score={h.score:.2f} rel={h.relevance:.2f}] {h.text}"
        )
    if below:
        lines.append(
            "note: confidence is below the calibration threshold — these matches "
            "may not actually answer the question. tell the lead you're blanking."
        )
    return _ok(
        "\n".join(lines),
        data={
            "confidence": confidence,
            "below_threshold": below,
            "threshold": threshold,
            "hits": [{
                "kind": h.kind, "ref_id": h.ref_id, "text": h.text,
                "score": h.score, "recency": h.recency,
                "importance": h.importance, "relevance": h.relevance,
            } for h in hits],
        },
    )


@tool(
    "remember",
    "Store a new atomic fact (subject + predicate + object) about the user or the world. "
    "Importance is 1-10 (1=trivial, 10=defining). Confidence is 0-1. "
    "If an active fact already exists for the same (subject, predicate), "
    "you must decide whether to: 'supersede' (new replaces old), "
    "'coexist' (both remain valid — e.g. multiple jobs), or 'merge' "
    "(new info refines old; caller updates the old fact). Default: supersede.",
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


@tool(
    "mark_fact_invalid",
    "Mark an existing fact as invalid (e.g. user said it was wrong) without a "
    "replacement. Use 'remember' with on_conflict='supersede' if you have a "
    "replacement fact.",
    {"fact_id": int, "reason": str},
)
async def mark_fact_invalid(args: dict[str, Any]) -> dict[str, Any]:
    fact_id = int(args.get("fact_id") or 0)
    if not fact_id:
        return _ok("mark_fact_invalid: fact_id is required.")
    reason = (args.get("reason") or "").strip() or None
    db.invalidate_fact(fact_id, reason=reason)
    return _ok(f"invalidated fact #{fact_id}.")


@tool(
    "update_core_block",
    "Update a labeled always-injected memory block (e.g. 'user_profile', "
    "'mood_today', 'preoccupation'). These are written to system context "
    "on every turn — keep them concise.",
    {"label": str, "content": str},
)
async def update_core_block(args: dict[str, Any]) -> dict[str, Any]:
    label = (args.get("label") or "").strip()
    content = (args.get("content") or "").strip()
    if not label:
        return _ok("update_core_block: label is required.")
    db.upsert_core_block(label, content)
    return _ok(f"core block {label!r} updated ({len(content)} chars).")


@tool(
    "task_create",
    "Create a new open loop / task / thing-to-follow-up-on. Open tasks are always "
    "injected into context so Hikari remembers what she owes the user.",
    {"subject": str, "description": str, "due_at": str},
)
async def task_create(args: dict[str, Any]) -> dict[str, Any]:
    subject = (args.get("subject") or "").strip()
    if not subject:
        return _ok("task_create: subject is required.")
    description = (args.get("description") or "").strip() or None
    due_at = (args.get("due_at") or "").strip() or None
    task_id = db.create_task(subject, description, due_at)
    return _ok(f"task #{task_id} created: {subject}")


@tool(
    "task_update",
    "Update a task's status. Valid statuses: pending, in_progress, completed, dropped.",
    {"task_id": int, "status": str},
)
async def task_update(args: dict[str, Any]) -> dict[str, Any]:
    task_id = int(args.get("task_id") or 0)
    status = (args.get("status") or "").strip().lower()
    if not task_id or status not in ("pending", "in_progress", "completed", "dropped"):
        return _ok("task_update: task_id and a valid status are required.")
    db.update_task(task_id, status=status)
    return _ok(f"task #{task_id} -> {status}")


ALL_TOOLS = [recall, remember, mark_fact_invalid, update_core_block, task_create, task_update]
