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
    "Search Hikari's PRIVATE memory of past chats and stored facts about the user "
    "(things they told her, their preferences, prior episodes). Returns ranked hits "
    "with a confidence score; below-threshold means 'don't fabricate, admit blanking'. "
    "e.g. user says 'remember when I told you about my sister' → call recall. "
    "Don't use this for the user's own notes (use wiki_search) or for public-web / "
    "current-events lookups (use the `research` subagent).",
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


@tool(
    "mark_fact_invalid",
    "Invalidate a stored fact by its numeric id (returned earlier by `recall`). "
    "Pass superseded_by=<new fact id> if there's a replacement, otherwise the row "
    "is just flagged invalid. e.g. user says 'I never said I hate cilantro, that was sarcastic' "
    "and recall returned fact #42 → mark_fact_invalid(42). "
    "Don't use this when you're inserting a new replacing fact in the same step — "
    "use `remember` with on_conflict='supersede' instead (one call, not two).",
    {"fact_id": int, "reason": str, "superseded_by": int},
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


@tool(
    "update_core_block",
    "Overwrite a labeled always-on memory block injected on every turn (e.g. "
    "'user_profile', 'mood_today', 'preoccupation'). Used sparingly — these are "
    "load-bearing system context. e.g. user explicitly redefines a stable trait: "
    "'actually my pronouns are they/them now' → update_core_block('user_profile', …). "
    "Don't use this for one-off facts (use `remember`) or for transient open loops "
    "(use `task_create`).",
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
    "Track a FUZZY open loop with NO real clock — something to follow up on "
    "'later' or 'next time we talk'. Open tasks are injected into context so Hikari "
    "remembers what she owes. e.g. user says 'don't let me forget to ask my mom "
    "about the recipe sometime' → task_create. "
    "Don't use this for time-bound reminders ('in 30 min', 'tomorrow at 9') — use "
    "`reminder_create` so a real push fires. Don't use this for a fact (use `remember`).",
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
    "Update an existing open-loop task by id. Valid statuses: pending, in_progress, "
    "completed, dropped. e.g. user just answered a follow-up you tracked earlier → "
    "task_update(id, 'completed'). Don't use this to create new tasks (use `task_create`) "
    "or to invalidate a fact (use `mark_fact_invalid`).",
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
