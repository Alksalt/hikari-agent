"""Anniversary callbacks: surface lexicon entries or significant events
with first_seen / event_date MMDD matching today's MMDD (±3 days).
Stage 3+ only. Max 1 per session.
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

from agents.engagement.triggers import TriggerCandidate

logger = logging.getLogger(__name__)


def _today_window_mmdd_set(window_days: int = 3) -> set[tuple[int, int]]:
    """Return set of (month, day) tuples within ±window_days of today."""
    today = date.today()
    out: set[tuple[int, int]] = set()
    for delta in range(-window_days, window_days + 1):
        d = today + timedelta(days=delta)
        out.add((d.month, d.day))
    return out


def collect() -> list[TriggerCandidate]:
    from agents import config as cfg
    from storage import db

    if not bool(cfg.get("engagement.anniversary_callback.enabled", True)):
        return []

    # Stage gate.
    min_stage = int(cfg.get("engagement.anniversary_callback.min_stage", 3))
    stage = db.get_relationship_stage()
    if stage < min_stage:
        return []

    # Per-session cap.
    last_session = db.runtime_get("anniversary_callback_last_session_id")
    current_session = db.get_session_id() or ""
    if last_session and last_session == current_session:
        return []

    window = _today_window_mmdd_set(
        window_days=int(cfg.get("engagement.anniversary_callback.window_days", 3))
    )
    today_year = date.today().year

    # (event_date, kind, summary, ref_key)
    candidates: list[tuple[date, str, str, str]] = []
    with db._conn() as c:
        # Query lexicon for first_seen_date matches.
        rows = c.execute(
            "SELECT phrase, first_seen_date FROM lexicon "
            "WHERE first_seen_date IS NOT NULL"
        ).fetchall()
        for r in rows:
            try:
                fs = datetime.fromisoformat(r["first_seen_date"]).date()
            except (ValueError, TypeError):
                continue
            if (fs.month, fs.day) not in window:
                continue
            # Only count if at least one year ago.
            if today_year - fs.year < 1:
                continue
            candidates.append(
                (fs, "lexicon", str(r["phrase"])[:120], f"lex:{r['phrase']}")
            )

        # significant_events table.
        rows2 = c.execute(
            "SELECT event_date, summary, kind FROM significant_events"
        ).fetchall()
        for r in rows2:
            try:
                ed = datetime.fromisoformat(r["event_date"]).date()
            except (ValueError, TypeError):
                continue
            if (ed.month, ed.day) not in window:
                continue
            if today_year - ed.year < 1:
                continue
            candidates.append(
                (
                    ed,
                    str(r["kind"]),
                    str(r["summary"])[:200],
                    f"evt:{r['event_date']}:{r['summary'][:30]}",
                )
            )

    if not candidates:
        return []

    # Pick oldest (most years back = strongest anniversary).
    candidates.sort(key=lambda x: x[0])
    event_date, kind, summary, ref_key = candidates[0]
    years_back = today_year - event_date.year

    return [
        TriggerCandidate(
            source="anniversary_callback",
            pool="agent_spontaneous",
            pattern="notify",
            payload={
                "anniversary_date": event_date.isoformat(),
                "years_back": years_back,
                "kind": kind,
                "summary": summary,
            },
            dedup_key=f"anniversary:{ref_key}",
            decay_at=datetime.now(UTC) + timedelta(hours=18),
            novelty=0.85,
            actionability=0.1,
            confidence=0.9,
        )
    ]


def mark_consumed(candidate: TriggerCandidate) -> None:
    """Bump per-session marker so we don't double-surface in one session."""
    from storage import db

    sid = db.get_session_id() or ""
    if sid:
        db.runtime_set("anniversary_callback_last_session_id", sid)
    else:
        logger.warning(
            "anniversary_callback.mark_consumed: no active session_id — per-session "
            "cap not written, producer will re-fire."
        )
