"""recall — search Hikari's private memory.

Pulls ranked hits from ``storage.retrieval`` and stamps a calibrated
confidence score on the result. Below-threshold confidence is the
explicit "don't fabricate, admit blanking" signal to the LLM.

The module-level ``retrieval`` re-export is load-bearing:
``tests/test_engagement_memory.py`` monkey-patches
``tools.memory.retrieval.retrieve`` to inject a synthetic hit list, and
that patch only lands if this handler resolves ``retrieval`` through the
package namespace (not a local ``from storage import retrieval`` shadow).
"""
from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from storage import retrieval
from tools._response import ok as _ok


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
