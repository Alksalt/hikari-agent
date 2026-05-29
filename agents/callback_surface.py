"""Callback surfacer — picks one "rememberable moment" topically adjacent to
the user's recent message and returns it so the inject_memory hook can drop
a hint block into Hikari's context. She decides whether to surface; her
assets/PERSONA.md 'i noticed —' rule already caps noticing to once per session, so
the upstream discipline is already in place.

Source rows: high-importance episodes within a 90-day window.
Scoring: token-overlap ratio against the recent user text.
Dedup: once-per-session via session_scratch (24h TTL).

Wave 2 additions:
  - ``framing_hint`` field on returned dict:
      i_keep_thinking  score>0.7, age 4-8 weeks, 30-turn guard
      act_from         score>0.6, age<14 days
      wrong_but_close  score<0.5
      implied          score≥0.4, age<30 days
  - Spaced-surprise multiplier 1.4 for items aged 28-60 days.
  - Pattern-language bias +0.2 ("always" / "every time" / "same way" / "keeps").
  - ``(approximate)`` annotation on text when score<0.5.
  - Anti-callback suppression when user prompt >120 chars and peer_model
    flags vulnerability in current_concerns.
  - Attribution multiplier read from peer_representation (episode-level stub;
    episodes don't have attribution so this is a no-op placeholder — kept for
    API parity with retrieval.py which does apply it to facts).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
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

# Pattern-language keywords — habitual/recurring phrases lift the score.
_PATTERN_LANGUAGE_RE = re.compile(
    r"\b(always|every time|same way|keeps)\b", re.IGNORECASE
)

# Spaced-surprise window.
_SURPRISE_MIN_DAYS = 28
_SURPRISE_MAX_DAYS = 60
_SURPRISE_MULTIPLIER = 1.4

# Vulnerability keywords searched in peer_representation.current_concerns.
_VULNERABILITY_KEYWORDS = frozenset({
    "upset", "hurt", "struggling", "scared", "scared", "crisis",
    "grieving", "breakdown", "overwhelmed", "anxious", "depressed",
    "panic", "distress", "vulnerable",
})

# runtime_state key tracked across turns to gate i_keep_thinking.
_LAST_I_KEEP_THINKING_KEY = "last_i_keep_thinking_at"

# runtime_state key tracked across turns to gate slow_burn_tells.
_LAST_SLOW_BURN_TELL_KEY = "last_slow_burn_tell_at"

# runtime_state key used to stash the picked slow-burn tell text between
# injection (hooks.py) and confirmed delivery (postsend.py).  C3 writes this
# key immediately after calling pick_slow_burn_tell(); postsend reads and
# clears it inside mark_pending_surfaced() to trigger mark_slow_burn_surfaced.
PENDING_SLOW_BURN_TELL_KEY = "pending_slow_burn_tell_text"


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


def _age_days(date_str: str) -> float:
    """Return the age of an episode in days from its ``date`` (YYYY-MM-DD) or
    ISO timestamp string. Returns a large value on parse failure."""
    if not date_str:
        return 1e6
    raw = str(date_str).strip()
    # Accept both plain date (YYYY-MM-DD) and full ISO timestamp.
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        try:
            ts = datetime.strptime(raw[:10], "%Y-%m-%d").replace(tzinfo=UTC)
        except (ValueError, TypeError):
            return 1e6
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - ts).total_seconds() / 86400.0)


def _pattern_language_bonus(text: str) -> float:
    """Return +0.2 if text contains habitual-pattern language."""
    return 0.2 if _PATTERN_LANGUAGE_RE.search(text or "") else 0.0


def _spaced_surprise_multiplier(age_days: float) -> float:
    """Return 1.4 if item is in the 28-60 day spaced-surprise window."""
    if _SURPRISE_MIN_DAYS <= age_days <= _SURPRISE_MAX_DAYS:
        return _SURPRISE_MULTIPLIER
    return 1.0


def _peer_model_flags_vulnerability() -> bool:
    """Return True if the peer_representation signals the user is currently
    in a vulnerable state (upset, scared, crisis, etc.).

    Checks ``current_concerns`` for vulnerability keywords.  Best-effort —
    returns False on any DB error so the check never suppresses callbacks
    due to a storage failure.
    """
    try:
        rep = db.get_peer_representation()
        if not rep:
            return False
        concerns = rep.get("current_concerns") or []
        text_blob = " ".join(str(c) for c in concerns).lower()
        return any(kw in text_blob for kw in _VULNERABILITY_KEYWORDS)
    except Exception:
        logger.debug("callback_surface: peer_model lookup failed (non-fatal)")
        return False


def _compute_framing_hint(
    score: float,
    age_days: float,
    turn_counter: int,
) -> str | None:
    """Derive the framing_hint for a candidate.

    Priority order (first match wins):
      i_keep_thinking  score>0.7, age 4-8 weeks (28-56 days), turn_counter 30+
      act_from         score>0.6, age<14 days
      wrong_but_close  score<0.5 (uncertain, Hikari makes a deliberate small slip)
      implied          score≥0.4, age<30 days
      None             no hint (score too low or conditions unmet)
    """
    if score > 0.7 and 28 <= age_days <= 56 and turn_counter >= 30:
        return "i_keep_thinking"
    if score > 0.6 and age_days < 14:
        return "act_from"
    if score < 0.5:
        return "wrong_but_close"
    if score >= 0.4 and age_days < 30:
        return "implied"
    return None


def pick_callback_candidate(recent_user_text: str) -> dict | None:
    """Return one callback dict or None.

    Shape: ``{id, text, source, date, score, framing_hint}``.
    ``framing_hint`` is one of: ``act_from`` / ``implied`` / ``wrong_but_close``
    / ``i_keep_thinking`` / ``None``.
    ``text`` gets an ``(approximate)`` suffix when score < 0.5.

    Anti-callback suppression: if the user prompt is >120 chars AND the
    peer_model flags vulnerability, callbacks are suppressed for this turn.
    """
    if not bool(cfg.get("callbacks.enabled", True)):
        return None
    if not recent_user_text or len(recent_user_text.strip()) < 4:
        return None

    # Anti-callback suppression: long message + vulnerable user → stay quiet.
    if len(recent_user_text) > 120 and _peer_model_flags_vulnerability():
        logger.debug("callback_surface: suppressed — long prompt + vulnerability flag")
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

    # Score each candidate, applying pattern-language bonus and
    # spaced-surprise multiplier.
    scored: list[tuple[float, dict[str, Any]]] = []
    for c in candidates:
        base = _score(c["text"], recent_user_text)
        age = _age_days(c["date"])
        adjusted = (base + _pattern_language_bonus(c["text"])) * _spaced_surprise_multiplier(age)
        scored.append((adjusted, c))

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

    # Compute framing_hint.  turns_since_last is derived from the inbound-message
    # counter delta so the 30-turn throttle resets after each emission.
    try:
        current_counter = db.runtime_get_int(db.INBOUND_MSG_COUNTER_KEY, 0)
        last_emit_at = db.runtime_get_int(_LAST_I_KEEP_THINKING_KEY, 0)
    except Exception:
        current_counter, last_emit_at = 0, 0
    turns_since_last = current_counter - last_emit_at

    age_days = _age_days(best["date"])
    # Re-compute base score (without multipliers) for framing decision.
    base_score = _score(best["text"], recent_user_text)
    framing_hint = _compute_framing_hint(base_score, age_days, turns_since_last)

    # Record the inbound counter at which we emitted, so the next call
    # sees `turns_since_last < 30` until 30 more inbound messages elapse.
    if framing_hint == "i_keep_thinking":
        try:
            current_counter = db.runtime_get_int(db.INBOUND_MSG_COUNTER_KEY, 0)
            db.runtime_set(_LAST_I_KEEP_THINKING_KEY, current_counter)
        except Exception:
            logger.debug(
                "callback_surface: failed to write %s (non-fatal)",
                _LAST_I_KEEP_THINKING_KEY,
            )

    # (approximate) annotation when score is low (below 0.5) so Hikari knows
    # to fuzz the recall — "wrong-but-close" tsundere rule.
    text_out = best["text"]
    if base_score < 0.5:
        text_out = f"{text_out} (approximate)"

    best["score"] = round(best_score, 3)
    best["framing_hint"] = framing_hint
    best["text"] = text_out
    return best


def pick_slow_burn_tell() -> dict | None:
    """Return the next session-milestone tell eligible to surface, or None.

    Gates:
    1. Feature enabled in config.
    2. db.session_count() >= tell's min_session_count.
    3. Cooldown: min_turns_between turns must have elapsed since last surface.
    4. Once-per-session dedup via session_scratch (topic "slow_burn_surfaced").

    Picks the highest-min_session_count eligible tell not yet surfaced this
    session (newest unlocked truth), falling back to the first eligible one.
    """
    if not cfg.get("slow_burn_tells.enabled", True):
        return None

    count = db.session_count()
    tells = cfg.get("slow_burn_tells.tells") or []
    if not tells:
        return None

    eligible = [t for t in tells if count >= int(t.get("min_session_count", 0))]
    if not eligible:
        return None

    current = db.runtime_get_int(db.INBOUND_MSG_COUNTER_KEY, 0)
    last = db.runtime_get_int(_LAST_SLOW_BURN_TELL_KEY, 0)
    min_between = int(cfg.get("slow_burn_tells.min_turns_between", 40))
    if last > 0 and current - last < min_between:
        return None

    session_id = db.get_session_id() or ""
    scratch_topic = "slow_burn_surfaced"
    already_surfaced: set[str] = set()
    if session_id:
        try:
            with db._conn() as conn:
                row = conn.execute(
                    "SELECT payload_json FROM session_scratch "
                    "WHERE session_id = ? AND topic = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (session_id, scratch_topic),
                ).fetchone()
            if row:
                already_surfaced = set(json.loads(row["payload_json"]).get("texts", []))
        except Exception:
            logger.exception("pick_slow_burn_tell: dedup read failed (non-fatal)")

    unsurfaced = [t for t in eligible if t.get("text", "") not in already_surfaced]
    if not unsurfaced:
        # All eligible tells already surfaced this session — stay silent. A tell
        # is said once across the arc, never re-surfaced after the cooldown.
        return None
    chosen = max(unsurfaced, key=lambda t: int(t.get("min_session_count", 0)))

    tell_text = chosen.get("text", "")
    gate = int(chosen.get("min_session_count", 0))

    # NOTE: cooldown write (_LAST_SLOW_BURN_TELL_KEY) and session_scratch dedup
    # are intentionally NOT written here.  They are deferred to
    # mark_slow_burn_surfaced(), called by postsend.mark_pending_surfaced() only
    # after the reply is confirmed delivered to the user.  This prevents the
    # "she finally said it" payoff from being consumed at injection time when the
    # model may choose not to surface the tell at all.

    return {
        "text": tell_text,
        "framing_hint": cfg.get("slow_burn_tells.framing_hint", "i_keep_thinking"),
        "min_session_count": gate,
    }


def mark_slow_burn_surfaced(tell_text: str) -> None:
    """Commit dedup + cooldown for a slow-burn tell AFTER it is confirmed
    emitted (post-send). Writes _LAST_SLOW_BURN_TELL_KEY and the
    session_scratch dedup row — the writes formerly done at pick time.

    Called by postsend.mark_pending_surfaced() when the delivered text
    contains the stashed slow-burn tell text, confirming the tell reached
    the user.
    """
    if not tell_text:
        return

    try:
        current = db.runtime_get_int(db.INBOUND_MSG_COUNTER_KEY, 0)
        db.runtime_set(_LAST_SLOW_BURN_TELL_KEY, current)
    except Exception:
        logger.debug(
            "mark_slow_burn_surfaced: failed to write %s (non-fatal)",
            _LAST_SLOW_BURN_TELL_KEY,
        )

    session_id = db.get_session_id() or ""
    if not session_id:
        return

    scratch_topic = "slow_burn_surfaced"
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
            already = set(json.loads(row["payload_json"]).get("texts", []))
        already.add(tell_text)
        with db._conn() as conn:
            conn.execute(
                "INSERT INTO session_scratch "
                "(session_id, topic, payload_json) VALUES (?, ?, ?)",
                (session_id, scratch_topic,
                 json.dumps({"texts": sorted(already)})),
            )
    except Exception:
        logger.exception("mark_slow_burn_surfaced: dedup write failed (non-fatal)")
