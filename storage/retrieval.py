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
import random
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

# Source → score multiplier. 'hikari' facts are dampened 0.7× to prevent
# Hikari's own statements from reinforcing themselves across sessions.
_SOURCE_MULTIPLIER: dict[str, float] = {
    "hikari": 0.7,
}

# Spaced-surprise window: items aged between these bounds get a lift.
_SURPRISE_MIN_DAYS = 28
_SURPRISE_MAX_DAYS = 60
_SURPRISE_MULTIPLIER = 1.4

# ---------------------------------------------------------------------------
# Phase M: ACT-R activation + Mem0 entity-match fusion
# ---------------------------------------------------------------------------

TAU_BY_CATEGORY: dict[str, float] = {
    "event":      3  * 86400,
    "preference": 21 * 86400,
    "fact":       29 * 86400,
}
TAU_DEFAULT_SECONDS = 29 * 86400
ACT_R_D = 0.5
ENTITY_BONUS_MAX = 0.3
_ACT_R_DEFAULT_EPSILON = 0.15
_QUERY_ENTITY_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_\-]{2,}\b")
_STOPWORDS = frozenset({
    "the", "and", "for", "you", "your", "was", "that", "this", "with",
    "from", "have", "has", "will", "but", "not", "are", "what", "when",
    "where", "who", "why", "how", "one", "two", "may", "can", "its",
})

# Predicate substring → TAU_BY_CATEGORY key.  Case-insensitive substring match
# is applied in order; first hit wins.  "fact" (the TAU_DEFAULT) is the fallback
# and is NOT listed here — _infer_category returns it explicitly.
_PREDICATE_CATEGORY_MAP: dict[str, str] = {
    "like":      "preference",
    "love":      "preference",
    "hate":      "preference",
    "prefer":    "preference",
    "favorite":  "preference",
    "enjoy":     "preference",
    "did":       "event",
    "went":      "event",
    "met":       "event",
    "happened":  "event",
    "attended":  "event",
    "finished":  "event",
    "started":   "event",
    "bought":    "event",
    "got":       "event",
}
_CATEGORY_DEFAULT = "fact"


def _infer_category(predicate: str) -> str:
    """Return the TAU_BY_CATEGORY key that best matches *predicate*.

    Performs a case-insensitive substring search against
    ``_PREDICATE_CATEGORY_MAP`` in insertion order.  Returns
    ``_CATEGORY_DEFAULT`` ("fact") when nothing matches.
    """
    lowered = predicate.lower()
    for keyword, category in _PREDICATE_CATEGORY_MAP.items():
        if keyword in lowered:
            return category
    return _CATEGORY_DEFAULT


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


def _source_multiplier(source: str | None) -> float:
    """0.7x for source='hikari' to prevent self-reinforcement. 1.0 otherwise."""
    return _SOURCE_MULTIPLIER.get((source or "").strip().lower(), 1.0)


def _spaced_surprise_multiplier(iso: str | None) -> float:
    """Return 1.4 if the item is aged 28-60 days (spaced-surprise window)."""
    hours = _hours_since(iso or "")
    days = hours / 24.0
    if _SURPRISE_MIN_DAYS <= days <= _SURPRISE_MAX_DAYS:
        return _SURPRISE_MULTIPLIER
    return 1.0


def _act_r_activation(
    age_seconds: float,
    hit_history_seconds: list[float],
    category: str | None,
    *,
    epsilon: float | None = None,
) -> float:
    """ACT-R activation as a relevance multiplier. Returns exp(A) clamped (0, ~3].

    Category-specific tau (seconds): event=3d, preference=21d, fact=29d.
    ``hit_history_seconds`` approximates spaced practice: each prior recall
    extends the activation sum. ``epsilon`` adds Gaussian noise for realistic
    variation; set to 0.0 in tests for determinism.
    """
    cat = (category or "").strip().lower()
    tau = TAU_BY_CATEGORY.get(cat, TAU_DEFAULT_SECONDS)
    times = [max(1.0, age_seconds / tau)] + [
        max(1.0, h / tau) for h in (hit_history_seconds or [])
    ]
    base = sum(t ** (-ACT_R_D) for t in times)
    if base <= 0:
        return 0.0
    A = math.log(base)
    eps = _ACT_R_DEFAULT_EPSILON if epsilon is None else float(epsilon)
    if eps > 0:
        A += random.gauss(0.0, eps)
    return math.exp(min(2.0, A))


def _extract_query_entity_ids(query: str) -> set[int]:
    """One SQL round-trip: tokenize the query, look up canonical_name + alias matches."""
    from storage import db as _db  # local import to mirror blueprint style
    tokens = {
        t.lower()
        for t in _QUERY_ENTITY_RE.findall(query or "")
        if len(t) >= 3
    }
    tokens -= _STOPWORDS
    if not tokens:
        return set()
    placeholders = ",".join("?" * len(tokens))
    sql = (
        f"SELECT id FROM entities WHERE lower(canonical_name) IN ({placeholders}) "
        f"UNION SELECT entity_id FROM entity_aliases WHERE lower(alias) IN ({placeholders})"
    )
    with _db._conn() as c:
        try:
            rows = c.execute(sql, (*tokens, *tokens)).fetchall()
        except Exception:
            return set()
    return {int(r["id"]) for r in rows}


def _facts_for_entity_ids(entity_ids: set[int]) -> set[int]:
    """One SQL round-trip: which fact_ids are linked to any of these entities."""
    from storage import db as _db
    if not entity_ids:
        return set()
    placeholders = ",".join("?" * len(entity_ids))
    with _db._conn() as c:
        try:
            rows = c.execute(
                f"SELECT DISTINCT fact_id FROM fact_entities "
                f"WHERE entity_id IN ({placeholders})",
                tuple(entity_ids),
            ).fetchall()
        except Exception:
            return set()
    return {int(r["fact_id"]) for r in rows}


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
            "source": rec.get("source"),
            "fact_category": rec.get("fact_category"),
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

    # Phase M: entity-match pre-fetch — one SQL round-trip before retrieval.
    query_entity_ids = _extract_query_entity_ids(query)
    boosted_fact_ids = _facts_for_entity_ids(query_entity_ids)

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

        # Phase M: ACT-R activation replaces Ebbinghaus for fact relevance.
        # Episodes already have a recency term in the base score, and the
        # recall_hit_count column doesn't exist on them.
        if kind == "fact":
            age = _seconds_since(rec.get("iso"))
            hit_count = int(rec.get("recall_hit_count") or 0)
            last_seen_age = (
                _seconds_since(rec.get("last_recalled_at"))
                if hit_count else age
            )
            hit_history = [last_seen_age] * min(hit_count, 5)
            activation = _act_r_activation(
                age_seconds=age,
                hit_history_seconds=hit_history,
                category=rec.get("fact_category"),
            )
            relevance *= activation

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

        # Phase N: source provenance multiplier — dampen hikari-authored facts
        # 0.7× to prevent Hikari's own statements from reinforcing themselves.
        score *= _source_multiplier(rec.get("source"))

        # Phase M: entity-match bonus — facts linked to entities in the query
        # get an additive ENTITY_BONUS_MAX boost on the final score.
        if kind == "fact" and ref_id in boosted_fact_ids:
            score += ENTITY_BONUS_MAX

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
