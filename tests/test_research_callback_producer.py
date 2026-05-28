"""Tests for agents.engagement.producers.research_callback."""
from __future__ import annotations

import importlib

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _setup(monkeypatch, tmp_path, *, stage: int = 3, mood: str = "focused"):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    from storage import db
    importlib.reload(db)
    db._reset_schema_sentinel()
    db.runtime_set("relationship_stage", stage)
    db.runtime_set("mood_today", mood)

    from agents.engagement.producers import research_callback
    importlib.reload(research_callback)
    return research_callback, db


def _seed_research_task(db, *, subject="look into transformers", summary=None,
                        surfaced_at=None, status="pending", research_intent=1):
    from datetime import UTC, datetime
    with db._conn() as c:
        cur = c.execute(
            "INSERT INTO tasks "
            "(subject, status, research_intent, research_summary, research_surfaced_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (subject, status, research_intent, summary, surfaced_at,
             datetime.now(UTC).isoformat()),
        )
    return cur.lastrowid


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_returns_empty_below_stage_3(tmp_path, monkeypatch):
    producer, db = _setup(monkeypatch, tmp_path, stage=2)
    _seed_research_task(db, summary="some summary")
    assert producer.collect() == []


def test_returns_empty_when_no_research_complete(tmp_path, monkeypatch):
    producer, db = _setup(monkeypatch, tmp_path, stage=3)
    _seed_research_task(db, summary=None)
    assert producer.collect() == []


def test_emits_when_summary_ready(tmp_path, monkeypatch):
    producer, db = _setup(monkeypatch, tmp_path, stage=3)
    task_id = _seed_research_task(db, subject="look into X", summary="Here is what I found.")
    candidates = producer.collect()
    assert len(candidates) == 1
    c = candidates[0]
    assert c.source == "research_callback"
    assert c.payload["task_id"] == task_id
    assert "look into X" in c.payload["subject"]
    assert "Here is what I found" in c.payload["summary_excerpt"]


def test_skips_empty_summary_marker(tmp_path, monkeypatch):
    producer, db = _setup(monkeypatch, tmp_path, stage=3)
    _seed_research_task(db, summary="(no useful sources)")
    assert producer.collect() == []


def test_skips_already_surfaced(tmp_path, monkeypatch):
    from datetime import UTC, datetime
    producer, db = _setup(monkeypatch, tmp_path, stage=3)
    _seed_research_task(
        db, summary="found something",
        surfaced_at=datetime.now(UTC).isoformat(),
    )
    assert producer.collect() == []


def test_mark_consumed_sets_surfaced_at(tmp_path, monkeypatch):
    producer, db = _setup(monkeypatch, tmp_path, stage=3)
    task_id = _seed_research_task(db, summary="some result")

    # Before mark_consumed it should appear.
    assert len(producer.collect()) == 1

    producer.mark_consumed(task_id=task_id)

    # After marking consumed it should be gone.
    assert producer.collect() == []
    with db._conn() as c:
        row = c.execute("SELECT research_surfaced_at FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["research_surfaced_at"] is not None


def test_blocked_mood_returns_empty(tmp_path, monkeypatch):
    producer, db = _setup(monkeypatch, tmp_path, stage=3, mood="irritable")
    _seed_research_task(db, summary="some summary")
    assert producer.collect() == []
