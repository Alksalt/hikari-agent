"""recall — search Hikari's private memory.

Phase D: primary read path uses Graphiti via ``storage.graph.search``.
Falls back to the legacy SQLite path (``storage.retrieval.legacy_retrieve``)
when the graph is unavailable (e.g. OPENROUTER_API_KEY missing, Kuzu init
failure). The fallback is logged at DEBUG level and is transparent to callers.

Graphiti ``EntityEdge.score`` → confidence buckets:
  score >= 0.75  →  HIGH_CONFIDENCE
  0.4 <= score < 0.75  →  MEDIUM_CONFIDENCE
  score < 0.4   →  LOW_CONFIDENCE

The module-level ``retrieval`` re-export is load-bearing:
``tests/test_engagement_memory.py`` monkey-patches
``tools.memory.retrieval.legacy_retrieve`` to inject a synthetic hit list
for fallback-path tests, and that patch only lands if this handler resolves
``retrieval`` through the package namespace (not a local shadow).
"""
from __future__ import annotations

import asyncio
import logging
import os as _os
from datetime import UTC as _UTC
from datetime import datetime as _datetime
from typing import Any

from claude_agent_sdk import tool

from agents import injection_guard
from storage import db as _db
from storage import graph as _graph
from storage import retrieval
from tools._annotations import annotations_for
from tools._response import ok as _ok

logger = logging.getLogger(__name__)

_HIGH = 0.75
_MED = 0.40


def _score_to_bucket(score: float) -> tuple[str, str]:
    """Return (bucket_label, prefix) for a graphiti edge score."""
    if score >= _HIGH:
        return "high", "HIGH_CONFIDENCE"
    if score >= _MED:
        return "medium", "MEDIUM_CONFIDENCE"
    return "low", "LOW_CONFIDENCE"


@tool(
    "recall",
    "Search Hikari's PRIVATE memory of past chats and stored facts about the user "
    "(things they told her, their preferences, prior episodes). Returns ranked hits "
    "with a confidence score; below-threshold means 'don't fabricate, admit blanking'. "
    "e.g. user says 'remember when I told you about my sister' → call recall. "
    "Don't use this for the user's own notes (use wiki_search) or for public-web / "
    "current-events lookups (use the `research` subagent).",
    {"query": str, "limit": int},
    annotations=annotations_for("recall"),
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

    threshold = float(cfg.get("recall_calibration.confidence_threshold", 0.6))

    # --- primary path: Graphiti ---
    # Short-circuit BEFORE calling graph.search when GRAPHITI_ENABLED=false
    # so no ERROR lines appear in logs (graph.search itself also no-ops, but
    # this skip avoids even the call overhead and the fallback debug log).
    if _os.environ.get("GRAPHITI_ENABLED", "true").strip().lower() in ("false", "0"):
        logger.debug("recall: GRAPHITI_ENABLED=false — skipping graph, using legacy_retrieve")
        return await _legacy_fallback(query, limit, threshold)

    try:
        edges = await _graph.search(query, group_id="hikari_chat", num_results=limit)
    except Exception:
        logger.debug("recall: graph.search raised for %r; falling back to legacy_retrieve", query)
        edges = []

    if not edges:
        # graph returned nothing — degrade to legacy SQLite
        logger.debug("recall: graph returned empty for %r; falling back to legacy_retrieve", query)
        return await _legacy_fallback(query, limit, threshold)

    # Filter out graph hits whose SQLite fact row is no longer active.
    # Edges that carry a fact_id (v1 payload) are validated against facts.status
    # and facts.valid_to. Edges with no fact_id are kept as expansion-only hits
    # (they may still add context) but never treated as the primary answer.
    now_iso = _datetime.now(_UTC).isoformat()
    active_edges: list = []
    expansion_edges: list = []
    for edge in edges:
        payload_fact_id = getattr(edge, "fact_id", None)
        if payload_fact_id is None:
            src_desc = str(getattr(edge, "source_description", "") or "")
            if src_desc.startswith("fact_id:"):
                try:
                    payload_fact_id = int(src_desc.split("|", 1)[0].split(":", 1)[1])
                except (ValueError, IndexError):
                    pass
        if payload_fact_id is None:
            expansion_edges.append(edge)
            continue
        try:
            fact_row = await asyncio.to_thread(_db.get_fact, int(payload_fact_id))
        except Exception:
            logger.debug("recall: get_fact(%s) failed; treating edge as expansion-only", payload_fact_id)
            expansion_edges.append(edge)
            continue
        if fact_row is None:
            # Row deleted — skip entirely
            logger.debug("recall: fact_id=%s not found in SQLite; dropping graph hit", payload_fact_id)
            continue
        status = fact_row.get("status", "active")
        valid_to = fact_row.get("valid_to")
        if status != "active" or (valid_to is not None and str(valid_to) < now_iso):
            logger.debug(
                "recall: dropping graph hit for fact_id=%s status=%r valid_to=%r",
                payload_fact_id, status, valid_to,
            )
            continue
        edge._sqlite_fact_id = int(payload_fact_id)
        active_edges.append(edge)

    # Primary answer requires at least one active edge.
    # Expansion-only edges are appended after for breadth, but confidence is
    # anchored on the top active edge score (or fallback if none).
    primary_edges = active_edges if active_edges else []
    if not primary_edges and not expansion_edges:
        logger.debug("recall: all graph hits filtered; falling back to legacy_retrieve")
        return await _legacy_fallback(query, limit, threshold)

    if not primary_edges:
        # Only expansion-only (no-fact_id) edges — degrade to legacy as primary,
        # caller will get SQLite answer with graph breadth unavailable.
        logger.debug("recall: only back-compat (no fact_id) graph hits; falling back to legacy")
        return await _legacy_fallback(query, limit, threshold)

    scored_edges = primary_edges + expansion_edges
    top_edge = scored_edges[0]
    top_score = float(getattr(top_edge, "score", 0.0))
    bucket_label, confidence_prefix = _score_to_bucket(top_score)
    below = top_score < threshold

    header = (
        f"{confidence_prefix}: {top_score:.2f} ({bucket_label}"
        f"{'; BELOW THRESHOLD' if below else ''}). "
        f"top {len(scored_edges)} graph matches for {query!r}:"
    )
    lines = [header]
    hit_data = []
    for edge in scored_edges:
        score = float(getattr(edge, "score", 0.0))
        fact_text = str(getattr(edge, "fact", "") or "")
        valid_at = getattr(edge, "valid_at", None)
        invalid_at = getattr(edge, "invalid_at", None)
        sqlite_fact_id = getattr(edge, "_sqlite_fact_id", None)

        time_note = ""
        if valid_at is not None:
            try:
                time_note = f" (recalled from {valid_at.strftime('%Y-%m-%d')})"
            except AttributeError:
                time_note = f" (recalled from {valid_at})"
        if invalid_at is not None:
            try:
                time_note += f" (expired {invalid_at.strftime('%Y-%m-%d')})"
            except AttributeError:
                time_note += f" (expired {invalid_at})"

        b_label, _ = _score_to_bucket(score)
        lines.append(f"  [graph score={score:.2f} conf={b_label}]{time_note} {fact_text}")
        hit_data.append({
            "fact": fact_text, "score": score,
            "fact_id": sqlite_fact_id,
            "valid_at": str(valid_at), "invalid_at": str(invalid_at),
            "attribution": None,
            "source_message_id": None,
            "source_span_hash": None,
            "recorded_at": None,
        })

    if below:
        lines.append(
            "note: confidence is below the calibration threshold — these matches "
            "may not actually answer the question. tell the lead you're blanking."
        )

    body = injection_guard.wrap_untrusted("recall.graph", "\n".join(lines))
    return _ok(
        body,
        data={
            "confidence": top_score,
            "below_threshold": below,
            "threshold": threshold,
            "source": "graph",
            "hits": hit_data,
        },
    )


async def _legacy_fallback(query: str, limit: int, threshold: float) -> dict[str, Any]:
    """Fallback to SQLite-backed legacy_retrieve when graphiti is unavailable."""
    try:
        hits = await asyncio.to_thread(retrieval.legacy_retrieve, query, limit)
    except Exception:
        logger.exception("recall: legacy_retrieve also failed for %r", query)
        return _ok(
            f"recall: both graph and legacy retrieval failed for {query!r}. "
            f"confidence: 0.00 (low). recommend telling the lead you don't remember.",
            data={"confidence": 0.0, "below_threshold": True, "source": "error", "hits": []},
        )

    if not hits:
        return _ok(
            f"recall: no memory matches for {query!r}. confidence: 0.00 (low). "
            f"recommend telling the lead you don't remember.",
            data={"confidence": 0.0, "below_threshold": True, "source": "legacy", "hits": []},
        )

    top = hits[0]
    hit_factor = min(1.0, len(hits) / 3.0)
    confidence = float(top.relevance) * hit_factor
    below = confidence < threshold
    bucket = "low" if confidence < 0.4 else "medium" if confidence < 0.7 else "high"
    if bucket == "high":
        prefix = "HIGH_CONFIDENCE"
    elif bucket == "medium":
        prefix = "MEDIUM_CONFIDENCE"
    else:
        prefix = "LOW_CONFIDENCE"
    header = (
        f"{prefix}: {confidence:.2f} ({bucket}"
        f"{'; BELOW THRESHOLD' if below else ''}). "
        f"top {len(hits)} matches for {query!r} [legacy sqlite]:"
    )
    lines = [header]
    hit_data = []
    for h in hits:
        lines.append(
            f"  [{h.kind}#{h.ref_id} score={h.score:.2f} rel={h.relevance:.2f}] {h.text}"
        )
        prov_fields: dict = {
            "attribution": None,
            "source_message_id": None,
            "source_span_hash": None,
            "recorded_at": None,
        }
        if h.kind == "fact":
            try:
                prov = _db.fact_provenance(int(h.ref_id))
                if prov:
                    prov_fields["attribution"] = prov.get("attribution")
                    prov_fields["source_message_id"] = prov.get("source_message_id")
                    prov_fields["source_span_hash"] = prov.get("source_span_hash")
                    prov_fields["recorded_at"] = prov.get("recorded_at")
            except Exception:
                logger.debug("recall: fact_provenance lookup failed for ref_id=%s", h.ref_id)
        hit_data.append({
            "kind": h.kind, "ref_id": h.ref_id, "text": h.text,
            "score": h.score, "recency": h.recency,
            "importance": h.importance, "relevance": h.relevance,
            **prov_fields,
        })
    if below:
        lines.append(
            "note: confidence is below the calibration threshold — these matches "
            "may not actually answer the question. tell the lead you're blanking."
        )
    body = injection_guard.wrap_untrusted("recall.legacy", "\n".join(lines))
    return _ok(
        body,
        data={
            "confidence": confidence,
            "below_threshold": below,
            "threshold": threshold,
            "source": "legacy",
            "hits": hit_data,
        },
    )
