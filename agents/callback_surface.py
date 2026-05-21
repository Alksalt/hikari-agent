"""Callback surfacer — picks one "rememberable moment" topically adjacent to
the user's recent message and returns it so the inject_memory hook can drop
a hint block into Hikari's context. She decides whether to surface; her
CLAUDE.md 'i noticed —' rule already caps noticing to once per session, so
the upstream discipline is already in place.

Source rows: high-importance episodes within a 90-day window.
Scoring: token-overlap ratio against the recent user text.
Dedup: once-per-session via session_scratch (24h TTL).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from agents import config as cfg
from storage import db

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "of", "in", "on", "at", "for",
    "with", "to", "from", "is", "was", "are", "be", "been", "i", "you",
    "your", "my", "me", "we", "they", "it", "this", "that", "those",
    "these", "have", "had", "has", "did", "do", "does",
})


def _tokens(text: str) -> set[str]:
    return {
        t.lower() for t in _TOKEN_RE.findall(text or "")
        if len(t) > 2 and t.lower() not in _STOPWORDS
    }


def _score(candidate_text: str, query_text: str) -> float:
    """Token-overlap score in [0, 1]. Ratio of unique query tokens that
    appear in the candidate."""
    q = _tokens(query_text)
    if not q:
        return 0.0
    c = _tokens(candidate_text)
    return len(q & c) / len(q)


def pick_callback_candidate(recent_user_text: str) -> dict | None:
    """Return one callback dict ``{id, text, source, date, score}`` or None.

    Pulls recent high-importance episodes, scores by token overlap with the
    user message, dedups against this session's scratch so we never surface
    the same row twice in one chat.
    """
    if not bool(cfg.get("callbacks.enabled", True)):
        return None
    if not recent_user_text or len(recent_user_text.strip()) < 4:
        return None

    min_importance = int(cfg.get("callbacks.min_importance", 6))
    min_score = float(cfg.get("callbacks.min_score", 0.25))
    window_days = int(cfg.get("callbacks.window_days", 90))

    candidates: list[dict[str, Any]] = []
    try:
        with db._conn() as conn:
            ep_rows = conn.execute(
                "SELECT id, date, summary FROM episodes "
                "WHERE importance >= ? "
                "AND date >= date('now', '-' || ? || ' days') "
                "ORDER BY date DESC LIMIT 50",
                (min_importance, window_days),
            ).fetchall()
        for r in ep_rows:
            candidates.append({
                "id": f"ep:{r['id']}",
                "source": "episode",
                "date": str(r["date"]),
                "text": str(r["summary"] or ""),
            })
    except Exception:
        logger.exception("callback_surface: episode query failed")

    if not candidates:
        return None

    scored = [(_score(c["text"], recent_user_text), c) for c in candidates]
    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best = scored[0]
    if best_score < min_score:
        return None

    # Session dedup: skip if we've already surfaced this candidate id in
    # this session.
    session_id = db.get_session_id() or ""
    if session_id:
        scratch_topic = "callback_surfaced"
        try:
            with db._conn() as conn:
                row = conn.execute(
                    "SELECT payload_json FROM session_scratch "
                    "WHERE session_id = ? AND topic = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (session_id, scratch_topic),
                ).fetchone()
            already: set[str] = set()
            if row:
                already = set(json.loads(row["payload_json"]).get("ids", []))
            if best["id"] in already:
                return None
            already.add(best["id"])
            with db._conn() as conn:
                conn.execute(
                    "INSERT INTO session_scratch "
                    "(session_id, topic, payload_json) VALUES (?, ?, ?)",
                    (session_id, scratch_topic,
                     json.dumps({"ids": sorted(already)})),
                )
        except Exception:
            logger.exception("callback_surface: dedup write failed")

    best["score"] = round(best_score, 3)
    return best
