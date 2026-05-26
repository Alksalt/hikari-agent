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

Wave 2 additions:
  - Spaced-surprise multiplier 1.4 for items aged 28-60 days.
  - Pattern-language bias +0.2 when text contains "always" / "every time" /
    "same way" / "keeps".
  - Attribution multiplier: user_stated 1.2, user_corrected 1.1, user_observed
    1.0, hikari_inferred 0.9, subagent_extracted 0.8, NULL/unknown 1.0.
  - vec_search pre-filtered to status='active' facts before hydration.
  - Buried lore rows excluded from scoring pipeline.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from tools import embeddings

from . import db

logger = logging.getLogger(__name__)

# Buried lore — these 5 canonical facts about Hikari must not surface through
# the normal recall pipeline (they're only revealed on direct question + second
# topic adjacency at chat surface).
_BURIED_LORE_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\bfailed paper\b", re.IGNORECASE),
    re.compile(r"\b3am playlist\b", re.IGNORECASE),
    re.compile(r"\bhikari.*city\b|\bcity.*hikari\b", re.IGNORECASE),
    re.compile(r"\brain.*cried\b|\bcried.*rain\b", re.IGNORECASE),
    re.compile(r"\blast time.*cried\b|\bcried.*last time\b", re.IGNORECASE),
)

# Pattern-language keywords that indicate a habitual / recurring behaviour.
_PATTERN_LANGUAGE_RE = re.compile(
    r"\b(always|every time|same way|keeps)\b", re.IGNORECASE
)

# Attribution → score multiplier table (NULL / unknown → 1.0).
_ATTRIBUTION_MULTIPLIER: dict[str, float] = {
    "user_stated":       1.2,
    "user_corrected":    1.1,
    "user_observed":     1.0,
    "hikari_inferred":   0.9,
    "subagent_extracted": 0.8,
}

# Spaced-surprise window: items aged between these bounds get a lift.
_SURPRISE_MIN_DAYS = 28
_SURPRISE_MAX_DAYS = 60
_SURPRISE_MULTIPLIER = 1.4


def _is_buried_lore(text: str) -> bool:
    """Return True if the text matches any buried-lore pattern."""
    for pat in _BURIED_LORE_PATTERNS:
        if pat.search(text):
            return True
    return False


def _pattern_language_bonus(text: str) -> float:
    """Return +0.2 if text contains habitual-pattern language, else 0.0."""
    return 0.2 if _PATTERN_LANGUAGE_RE.search(text or "") else 0.0


def _attribution_multiplier(attribution: str | None) -> float:
    """Map attribution tag → score multiplier. Unknown/NULL → 1.0."""
    if not attribution:
        return 1.0
    return _ATTRIBUTION_MULTIPLIER.get(attribution.strip(), 1.0)


def _spaced_surprise_multiplier(iso: str | None) -> float:
    """Return 1.4 if the item is aged 28-60 days (spaced-surprise window)."""
    hours = _hours_since(iso or "")
    days = hours / 24.0
    if _SURPRISE_MIN_DAYS <= days <= _SURPRISE_MAX_DAYS:
        return _SURPRISE_MULTIPLIER
    return 1.0


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


# Module-level config read. The lazy import inside ``retrieve()`` predates
# this and exists for a different reason (test fixtures that reload
# ``storage.db`` before ``agents.config`` loads); the constants below tolerate
# that case via a try/except fallback to the historical defaults.
try:
    from agents import config as cfg

    W_RECENCY = cfg.get("retrieval.w_recency") or 1.0
    W_IMPORTANCE = cfg.get("retrieval.w_importance") or 1.0
    W_RELEVANCE = cfg.get("retrieval.w_relevance") or 1.5
    RECENCY_DECAY_PER_HOUR = cfg.get("retrieval.recency_decay_per_hour") or 0.99
    VEC_K = cfg.get("retrieval.vec_k") or 30
    BM25_K = cfg.get("retrieval.bm25_k") or 30
    HYBRID_VEC_WEIGHT = cfg.get("retrieval.hybrid_vec_weight") or 0.6
    HYBRID_BM25_WEIGHT = cfg.get("retrieval.hybrid_bm25_weight") or 0.4
except Exception:
    W_RECENCY = 1.0
    W_IMPORTANCE = 1.0
    W_RELEVANCE = 1.5
    RECENCY_DECAY_PER_HOUR = 0.99
    VEC_K = 30
    BM25_K = 30
    HYBRID_VEC_WEIGHT = 0.6
    HYBRID_BM25_WEIGHT = 0.4


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
        if not rec:
            return None
        # T3.1 — bi-temporal active check: drop the candidate if it's been
        # invalidated/superseded (``valid_to`` set in the past). A future
        # ``valid_to`` is still active; this mirrors the SQL predicate
        # ``valid_to IS NULL OR valid_to > datetime('now')``.
        if not _fact_active(rec.get("valid_to")):
            return None
        # Defense in depth — also drop rows whose explicit status was flipped
        # but whose ``valid_to`` wasn't (shouldn't happen, but cheap to check).
        status = (rec.get("status") or "active")
        if status not in ("active", ""):
            return None
        text = f"{rec['subject']} {rec['predicate']} {rec['object']}"
        # Buried lore exclusion gate: never surface these through the normal
        # pipeline — they're guarded at chat surface with a stricter gate.
        if _is_buried_lore(text):
            return None
        iso = rec.get("valid_from") or rec.get("created_at")
        importance = rec.get("importance") or 5
        return {
            "text": text, "iso": iso, "importance": int(importance),
            "last_recalled_at": rec.get("last_recalled_at"),
            "recall_hit_count": int(rec.get("recall_hit_count") or 0),
            "attribution": rec.get("attribution"),
        }
    elif kind == "episode":
        rec = db.get_episode(ref_id)
        if not rec:
            return None
        text = rec["summary"]
        # Buried lore exclusion gate applies to episodes too.
        if _is_buried_lore(text):
            return None
        iso = rec.get("created_at")
        importance = rec.get("importance") or 5
        return {"text": text, "iso": iso, "importance": int(importance),
                "attribution": None}
    return None


def _fact_active(valid_to: Any) -> bool:
    """Return True when a fact row should be considered live right now.

    Mirrors the SQL predicate ``valid_to IS NULL OR valid_to > datetime('now')``.
    Malformed timestamps are treated as expired — conservative for retrieval.
    """
    if valid_to is None:
        return True
    raw = str(valid_to).strip()
    if not raw:
        return True
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts > datetime.now(UTC)


def _ebbinghaus_multiplier(
    last_seen_iso: str | None,
    hit_count: int,
    tau_base_seconds: float,
) -> float:
    """T3.2 — exponential forgetting curve.

    ``tau = tau_base * 1.5 ** hit_count`` — each successful recall stretches
    the half-life so frequently-touched facts decay slower (rehearsal effect).
    ``delta`` is seconds since ``last_seen_iso``; the result is
    ``exp(-delta / tau)`` and lives in ``(0, 1]``.

    A malformed or missing timestamp is treated as "infinitely old" — the
    multiplier collapses toward zero and the fact loses ranking weight, which
    is the conservative choice for unknown freshness.
    """
    if tau_base_seconds <= 0:
        return 1.0
    delta = _seconds_since(last_seen_iso)
    tau = tau_base_seconds * (1.5 ** max(0, int(hit_count)))
    if tau <= 0:
        return 0.0
    # Cap the exponent to avoid math.exp underflow on very stale rows; the
    # value drops to ~1e-300 well before the cap, so this is just hygiene.
    exponent = -min(700.0, delta / tau)
    return math.exp(exponent)


def _seconds_since(iso: str | None) -> float:
    if not iso:
        return float("inf")
    raw = str(iso).strip()
    if not raw:
        return float("inf")
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return float("inf")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - ts).total_seconds())


def legacy_retrieve(query: str, limit: int = 8) -> list[Hit]:
    """Retrieve top-N hits across facts + episodes using hybrid scoring.

    Renamed from ``retrieve`` in Phase D. Kept for one release as the
    rollback path when graphiti reads degrade. New call sites should use
    ``storage.graph.search`` instead.

    T3.2 layers an Ebbinghaus forgetting curve over the relevance signal:
    each fact's relevance is multiplied by ``exp(-delta / tau)`` where
    ``delta`` is seconds since last recall (or creation if never recalled)
    and ``tau`` grows with ``recall_hit_count`` so frequently-touched facts
    decay slower. Episodes are not decayed — they're already time-stamped
    via the existing ``recency`` term.

    After ranking, the returned facts have their ``last_recalled_at`` +
    ``recall_hit_count`` bumped so the next call sees the updated tau.
    """
    query = (query or "").strip()
    if not query:
        return []

    # Lazy config import — avoids a hard dependency at import time which would
    # break test fixtures that reload storage.db before agents.config loads.
    try:
        from agents import config as cfg
        tau_base = float(
            cfg.get("memory.recall_decay_tau_seconds", 604800)
        )
    except Exception:
        tau_base = 604800.0

    bm25_rank: dict[tuple[str, int], float] = {}
    vec_dist: dict[tuple[str, int], float] = {}

    # BM25
    for r in db.fts_search(query, limit=BM25_K):
        key = (r["kind"], int(r["ref_id"]))
        bm25_rank[key] = float(r["rank"])

    # Vector — facts use vec_search_active_facts (pre-filtered to status='active')
    # to avoid hydrating invalidated/superseded rows.  Episodes don't have a
    # status column so the standard vec_search path is kept for them.
    try:
        q_emb = embeddings.embed(query)
        for v in db.vec_search_active_facts(q_emb, k=VEC_K):
            key = ("fact", int(v["id"]))
            vec_dist[key] = float(v["distance"])
        for v in db.vec_search("vec_episodes", q_emb, k=VEC_K):
            key = ("episode", int(v["id"]))
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
            relevance = HYBRID_VEC_WEIGHT * vn + HYBRID_BM25_WEIGHT * bn
        elif vn is not None:
            relevance = vn
        elif bn is not None:
            relevance = bn
        else:
            relevance = 0.0

        # Ebbinghaus decay applies to fact relevance only. Episodes already
        # have a recency term in the base score, and the recall_hit_count
        # column doesn't exist on them.
        if kind == "fact":
            last_seen = rec.get("last_recalled_at") or rec.get("iso")
            hit_count = int(rec.get("recall_hit_count") or 0)
            decay_multiplier = _ebbinghaus_multiplier(
                last_seen, hit_count, tau_base
            )
            relevance *= decay_multiplier

        recency = _recency_score(rec["iso"] or "")
        importance = rec["importance"] / 10.0
        score = (W_RECENCY * recency
                 + W_IMPORTANCE * importance
                 + W_RELEVANCE * relevance)

        # Wave 2: pattern-language bias — recurring / habitual text gets +0.2
        # on the raw score so "she always does X" ranks above single-occurrence facts.
        score += _pattern_language_bonus(rec["text"])

        # Wave 2: spaced-surprise multiplier — items aged 28-60 days feel more
        # surprising to surface, which is exactly when they should be.
        score *= _spaced_surprise_multiplier(rec.get("iso"))

        # Wave 2: attribution multiplier — trust the source; user_stated facts
        # outrank hikari-inferred or subagent-extracted ones.
        score *= _attribution_multiplier(rec.get("attribution"))

        hits.append(Hit(
            kind=kind, ref_id=ref_id, text=rec["text"],
            iso_ts=rec["iso"] or "",
            score=score, recency=recency,
            importance=importance, relevance=relevance,
        ))

    hits.sort(key=lambda h: h.score, reverse=True)
    top = hits[:limit]

    # Bump access counters for the returned facts so the next recall sees a
    # stretched tau. Best-effort — a DB write failure here must not break the
    # recall return path (worst case the decay simply doesn't update).
    fact_ids = [h.ref_id for h in top if h.kind == "fact"]
    if fact_ids:
        try:
            db.facts_mark_recalled(fact_ids)
        except Exception:
            logger.exception("facts_mark_recalled failed (non-fatal)")

    return top
