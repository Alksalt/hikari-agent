"""Park et al. retrieval scoring with hybrid cosine + BM25.

Formula:
  score = w_recency * recency_score
        + w_importance * importance_score
        + w_relevance * relevance

Where relevance is:
  - if both signals present: 0.6 * cosine_norm + 0.4 * bm25_norm
  - else: whichever single signal is present

Candidates are the union of vec0 KNN top-30 and FTS5 BM25 top-30, across facts
and episodes. If embeddings aren't available (model fails to load), we fall back
to BM25-only — same retrieval shape, slightly worse semantic matching.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from tools import embeddings

from . import db

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Hit:
    kind: str               # 'fact' | 'episode'
    ref_id: int
    text: str
    iso_ts: str             # valid_from for facts, created_at for episodes
    score: float
    recency: float
    importance: float
    relevance: float


W_RECENCY = 1.0
W_IMPORTANCE = 1.0
W_RELEVANCE = 1.5
RECENCY_DECAY_PER_HOUR = 0.99

VEC_K = 30
BM25_K = 30


def _hours_since(iso: str) -> float:
    try:
        ts = datetime.fromisoformat(iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return max(0.0, (datetime.now(UTC) - ts).total_seconds() / 3600)
    except (ValueError, TypeError):
        return 1e6


def _recency_score(iso: str) -> float:
    return math.pow(RECENCY_DECAY_PER_HOUR, _hours_since(iso))


def _normalize_lower_is_better(values: dict[Any, float]) -> dict[Any, float]:
    """Lower input = better. Returns [0,1] where 1 = best across the set."""
    if not values:
        return {}
    vmin = min(values.values())
    vmax = max(values.values())
    span = vmax - vmin
    if span == 0:
        return dict.fromkeys(values, 1.0)
    return {k: 1.0 - (v - vmin) / span for k, v in values.items()}


def _hydrate(kind: str, ref_id: int) -> dict[str, Any] | None:
    if kind == "fact":
        rec = db.get_fact(ref_id)
        if not rec or rec.get("valid_to"):
            return None
        text = f"{rec['subject']} {rec['predicate']} {rec['object']}"
        iso = rec.get("valid_from") or rec.get("created_at")
        importance = rec.get("importance") or 5
    elif kind == "episode":
        rec = db.get_episode(ref_id)
        if not rec:
            return None
        text = rec["summary"]
        iso = rec.get("created_at")
        importance = rec.get("importance") or 5
    else:
        return None
    return {"text": text, "iso": iso, "importance": int(importance)}


def retrieve(query: str, limit: int = 8) -> list[Hit]:
    """Retrieve top-N hits across facts + episodes using hybrid scoring."""
    query = (query or "").strip()
    if not query:
        return []

    bm25_rank: dict[tuple[str, int], float] = {}
    vec_dist: dict[tuple[str, int], float] = {}

    # BM25
    for r in db.fts_search(query, limit=BM25_K):
        key = (r["kind"], int(r["ref_id"]))
        bm25_rank[key] = float(r["rank"])

    # Vector
    try:
        q_emb = embeddings.embed(query)
        for kind, table in (("fact", "vec_facts"), ("episode", "vec_episodes")):
            for v in db.vec_search(table, q_emb, k=VEC_K):
                key = (kind, int(v["id"]))
                vec_dist[key] = float(v["distance"])
    except Exception:
        logger.exception("vector retrieval failed; falling back to BM25 only")

    keys = set(bm25_rank) | set(vec_dist)
    if not keys:
        return []

    bm25_norm = _normalize_lower_is_better(bm25_rank)
    vec_norm = _normalize_lower_is_better(vec_dist)

    hits: list[Hit] = []
    for key in keys:
        kind, ref_id = key
        rec = _hydrate(kind, ref_id)
        if not rec:
            continue

        bn = bm25_norm.get(key)
        vn = vec_norm.get(key)
        if bn is not None and vn is not None:
            relevance = 0.6 * vn + 0.4 * bn
        elif vn is not None:
            relevance = vn
        elif bn is not None:
            relevance = bn
        else:
            relevance = 0.0

        recency = _recency_score(rec["iso"] or "")
        importance = rec["importance"] / 10.0
        score = (W_RECENCY * recency
                 + W_IMPORTANCE * importance
                 + W_RELEVANCE * relevance)

        hits.append(Hit(
            kind=kind, ref_id=ref_id, text=rec["text"],
            iso_ts=rec["iso"] or "",
            score=score, recency=recency,
            importance=importance, relevance=relevance,
        ))

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]
