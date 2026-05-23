"""Stage-2 memory engagement tests: lexicon, handoff, recall calibration, task decay.

Each test runs against a per-test SQLite DB (via HIKARI_DB_PATH monkeypatch) so
state never leaks between tests.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agents import config, handoff, hooks, lexicon_extractor
from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    """Fresh DB + fresh config cache per test."""
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    # Force db module to re-resolve _DB_PATH.
    import importlib

    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)  # belt and braces
    config.reload()
    yield


# ---------- lexicon table + helpers ----------

def test_lexicon_record_insert_and_bump():
    rid = db.lexicon_record("attention sinks", source="mutual")
    assert rid > 0
    again = db.lexicon_record("attention sinks", source="user_coined")
    assert again == rid  # same row reused on conflict
    row = db.lexicon_get("attention sinks")
    assert row is not None
    assert int(row["mention_count"]) == 2
    assert float(row["weight"]) > 0.5  # weight bumped on re-record


def test_lexicon_top_orders_by_score():
    db.lexicon_record("recent phrase", source="user_coined")
    db.lexicon_record("recent phrase")  # bump weight + recency
    db.lexicon_record("recent phrase")

    # Insert a separate older phrase manually with a fixed older timestamp.
    with db._conn() as c:
        old_iso = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        c.execute(
            "INSERT INTO lexicon "
            "(phrase, source, weight, mention_count, last_used_at, created_at) "
            "VALUES (?, 'user_coined', 0.9, 1, ?, ?)",
            ("old phrase", old_iso, old_iso),
        )

    top = db.lexicon_top(limit=2, half_life_days=14.0)
    assert top
    # The recent phrase should outrank the old one because of exponential decay,
    # even though "old phrase" started with higher weight.
    assert top[0]["phrase"] == "recent phrase"


def test_lexicon_top_empty_table():
    assert db.lexicon_top(limit=5) == []


# ---------- lexicon extractor ----------

def test_lexicon_extractor_promotes_repeated_phrase():
    # Insert several user messages that share a distinctive phrase.
    for _ in range(3):
        db.append_message("user", "the cabbage thing is back again")
    promoted = lexicon_extractor.extract_and_promote(lookback_days=7)
    # We expect at least one new phrase to make it through (e.g. "cabbage thing").
    assert promoted >= 1
    # Confirm "cabbage thing" specifically is in the lexicon.
    rows = [db.lexicon_get(p) for p in ("the cabbage", "cabbage thing", "cabbage thing is")]
    assert any(r is not None for r in rows)


def test_lexicon_extractor_excludes_stopword_starts():
    # All these start with stopwords / excluded heads and should not promote.
    for _ in range(3):
        db.append_message("user", "the the the")
    promoted = lexicon_extractor.extract_and_promote(lookback_days=7)
    assert promoted == 0


def test_lexicon_extractor_silent_when_disabled(monkeypatch, tmp_path):
    cfg_text = "lexicon:\n  enabled: false\n"
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()
    for _ in range(5):
        db.append_message("user", "shouldnt promote this phrase ever")
    assert lexicon_extractor.extract_and_promote() == 0


# ---------- session handoff ----------

def test_handoff_write_and_consume():
    db.append_message("user", "the meeting tomorrow")
    db.append_message("assistant", "fine. i'll prep.")
    handoff.write_handoff()

    # Make peek/consume see a stale-enough handoff by rewriting the stored ts
    # to 1 hour ago (peek_handoff hides mid-session handoffs <30min old).
    raw = db.runtime_get("session_handoff")
    assert raw, "handoff should be written"
    data = json.loads(raw)
    data["ts"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    db.runtime_set("session_handoff", json.dumps(data))

    consumed = handoff.consume_handoff()
    assert consumed is not None
    assert len(consumed["turns"]) >= 1
    # Second consume should return None (cleared).
    assert handoff.consume_handoff() is None


def test_handoff_stale_is_discarded():
    db.append_message("user", "stale")
    handoff.write_handoff()
    raw = db.runtime_get("session_handoff")
    assert raw
    data = json.loads(raw)
    data["ts"] = (datetime.now(UTC) - timedelta(hours=72)).isoformat()
    db.runtime_set("session_handoff", json.dumps(data))
    assert handoff.consume_handoff() is None
    # Stale entries get cleared on peek.
    assert db.runtime_get("session_handoff") is None


def test_handoff_format_includes_turns():
    # SPASM Egocentric Context Projection (arxiv 2604.09212) rewrites
    # USER:/ASSISTANT: into [partner]:/[self]: so the model reads the handoff
    # as first-person memory. Contract verified here is post-projection.
    snapshot = {
        "ts": "2026-05-19T12:00:00+00:00",
        "turns": [
            {"role": "user", "content": "you up?"},
            {"role": "assistant", "content": "barely."},
        ],
    }
    out = handoff.format_for_injection(snapshot)
    assert "[partner]: you up?" in out
    assert "[self]: barely." in out


# ---------- hooks injection wiring ----------

def test_hooks_format_lexicon_returns_top_entry():
    db.lexicon_record("attention sinks", source="mutual")
    db.lexicon_record("attention sinks")  # bump weight
    formatted = hooks._format_lexicon()
    assert "attention sinks" in formatted


def test_hooks_format_lexicon_empty_when_below_threshold(monkeypatch, tmp_path):
    cfg_text = (
        "lexicon:\n"
        "  enabled: true\n"
        "  inject_top_n_per_turn: 1\n"
        "  inject_min_score: 0.99\n"
        "  recency_half_life_days: 14\n"
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()
    db.lexicon_record("low weight phrase")
    assert hooks._format_lexicon() == ""


# ---------- recall confidence calibration ----------

@pytest.mark.asyncio
async def test_recall_returns_zero_confidence_on_empty_db():
    from tools.memory import recall
    res = await recall.handler({"query": "anything"})
    data = res.get("data") or {}
    assert data.get("confidence") == 0.0
    assert data.get("below_threshold", True) is True
    body = res["content"][0]["text"]
    assert "no memory matches" in body.lower()


def test_lexicon_decay_and_prune_floors_weight():
    """Repeated decay should drop entries below the floor."""
    rid = db.lexicon_record("attention sinks")
    # Brand-new entry starts at weight 0.5.
    # Apply heavy decay several times until below the 0.05 floor.
    for _ in range(50):
        db.lexicon_decay_and_prune(decay_per_call=0.02, min_weight=0.05)
    row = db.lexicon_get("attention sinks")
    assert row is None, f"entry should be pruned after heavy decay, got {row}"
    _ = rid  # appease unused


def test_recall_confidence_scales_with_hit_count(monkeypatch):
    """Cold-start guard: a single high-relevance SQLite hit in the legacy
    fallback must not self-certify as confidence=1.0 due to within-pool
    normalization. Exercises the fallback path (graph returns empty)."""
    import asyncio
    import importlib
    from unittest.mock import AsyncMock

    import tools.memory as mem_mod

    from storage.retrieval import Hit

    fake_hit = Hit(
        kind="fact", ref_id=1, text="x", iso_ts="2026-01-01T00:00:00+00:00",
        score=1.0, recency=1.0, importance=0.5, relevance=1.0,
    )
    # Graph returns empty → fallback fires.
    monkeypatch.setattr("storage.graph.search", AsyncMock(return_value=[]))
    monkeypatch.setattr(mem_mod.retrieval, "legacy_retrieve", lambda q, limit=8: [fake_hit])

    res = asyncio.run(mem_mod.recall.handler({"query": "x"}))
    data = res["data"]
    # With only 1 hit, confidence is scaled by 1/3 ≈ 0.333, not 1.0.
    assert data["confidence"] < 0.5, (
        f"single-hit confidence must be guarded; got {data['confidence']}"
    )
    importlib.reload(mem_mod)


# ---------- open-loop decay ----------

def test_task_decay_drops_old_low_importance_task():
    # Importance 1 → half-life 2 days, decay horizon = 4 days. Insert with
    # a created_at 10 days ago.
    old_iso = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    with db._conn() as c:
        cur = c.execute(
            "INSERT INTO tasks (subject, status, importance, created_at) "
            "VALUES (?, 'pending', 1, ?)",
            ("ancient trivial task", old_iso),
        )
        tid = cur.lastrowid

    decayed, mention_dropped = db.task_decay_sweep(
        half_life_by_importance={1: 2, 5: 14, 10: 180},
        default_half_life_days=14,
        max_mentions_before_drop=2,
    )
    assert decayed >= 1
    # Confirm the task is now dropped.
    with db._conn() as c:
        row = c.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row["status"] == "dropped"


def test_task_decay_drops_over_mentioned_task():
    tid = db.create_task("recurring nag", importance=8)
    db.task_record_mention(tid)
    db.task_record_mention(tid)  # cap reached
    decayed, mention_dropped = db.task_decay_sweep(
        half_life_by_importance={8: 60},
        default_half_life_days=14,
        max_mentions_before_drop=2,
    )
    assert mention_dropped >= 1
    with db._conn() as c:
        row = c.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row["status"] == "dropped"


def test_task_decay_preserves_recent_high_importance():
    tid = db.create_task("important", importance=10)
    decayed, mention_dropped = db.task_decay_sweep(
        half_life_by_importance={10: 180},
        default_half_life_days=14,
        max_mentions_before_drop=2,
    )
    assert decayed == 0
    assert mention_dropped == 0
    with db._conn() as c:
        row = c.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row["status"] == "pending"
