"""Phase D — recall reads from Graphiti + legacy fallback tests.

All graph calls are mocked (no real Kuzu / LLM). Backfill script tests use
a temporary SQLite DB and a mocked add_episode_safe.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.uses_real_graph


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    """Fresh per-test DB so recall's SQLite fact-validity lookups work."""
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from agents import config
    config.reload()
    yield
    config.reload()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _edge(fact: str, score: float, valid_at=None, invalid_at=None, fact_id=None) -> MagicMock:
    e = MagicMock()
    e.fact = fact
    e.score = score
    e.valid_at = valid_at
    e.invalid_at = invalid_at
    # Explicitly set fact_id so back-compat (no fact_id) path is tested by callers
    # that don't pass it, and validity-gate tests can pass a real SQLite id.
    e.fact_id = fact_id
    return e


def _insert_fact(subject: str = "user", predicate: str = "test", obj: str = "fact") -> int:
    from storage import db
    return db.insert_fact(subject, predicate, obj)


async def _call_recall(query: str, limit: int = 8) -> dict:
    from tools.memory.recall import recall
    return await recall.handler({"query": query, "limit": limit})


def _text(result: dict) -> str:
    return result["content"][0]["text"]


# ---------------------------------------------------------------------------
# Test 1 — HIGH_CONFIDENCE when score >= 0.75
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recall_returns_high_confidence_when_score_high(monkeypatch):
    fid = _insert_fact("user", "loves", "matcha")
    edge = _edge("user loves matcha", score=0.9, fact_id=fid)
    monkeypatch.setattr("storage.graph.search", AsyncMock(return_value=[edge]))

    result = await _call_recall("matcha")
    text = _text(result)
    assert "HIGH_CONFIDENCE" in text
    assert result["data"]["confidence"] == pytest.approx(0.9)
    assert result["data"]["source"] == "graph"


# ---------------------------------------------------------------------------
# Test 2 — MEDIUM_CONFIDENCE when 0.4 <= score < 0.75
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recall_returns_medium_confidence_when_score_mid(monkeypatch):
    fid = _insert_fact("user", "has_sister", "Yuki")
    edge = _edge("user has a sister named Yuki", score=0.5, fact_id=fid)
    monkeypatch.setattr("storage.graph.search", AsyncMock(return_value=[edge]))

    result = await _call_recall("sister")
    text = _text(result)
    assert "MEDIUM_CONFIDENCE" in text
    assert result["data"]["confidence"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Test 3 — LOW_CONFIDENCE when score < 0.4
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recall_returns_low_confidence_when_score_low(monkeypatch):
    fid = _insert_fact("user", "mentioned", "Oslo")
    edge = _edge("user once mentioned Oslo", score=0.2, fact_id=fid)
    monkeypatch.setattr("storage.graph.search", AsyncMock(return_value=[edge]))

    result = await _call_recall("oslo")
    text = _text(result)
    assert "LOW_CONFIDENCE" in text
    assert result["data"]["confidence"] == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# Test 4 — falls back to legacy when graph returns empty
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recall_falls_back_to_legacy_when_graph_returns_empty(monkeypatch):
    from storage.retrieval import Hit

    legacy_hit = Hit(
        kind="fact",
        ref_id=1,
        text="user studies attention mechanisms",
        iso_ts="2026-01-01T00:00:00+00:00",
        score=2.1,
        recency=0.9,
        importance=0.7,
        relevance=0.8,
    )
    monkeypatch.setattr("storage.graph.search", AsyncMock(return_value=[]))
    monkeypatch.setattr("storage.retrieval.legacy_retrieve", lambda q, limit=8: [legacy_hit])

    result = await _call_recall("attention")
    text = _text(result)
    assert "attention mechanisms" in text
    assert result["data"]["source"] == "legacy"


# ---------------------------------------------------------------------------
# Test 5 — falls back to legacy when graph raises
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recall_falls_back_to_legacy_when_graph_raises(monkeypatch):
    from storage.retrieval import Hit

    legacy_hit = Hit(
        kind="fact",
        ref_id=2,
        text="user moved to oslo",
        iso_ts="2026-02-01T00:00:00+00:00",
        score=1.8,
        recency=0.85,
        importance=0.6,
        relevance=0.75,
    )

    async def _raise(*a, **kw):
        raise RuntimeError("graph unavailable")

    monkeypatch.setattr("storage.graph.search", _raise)
    monkeypatch.setattr("storage.retrieval.legacy_retrieve", lambda q, limit=8: [legacy_hit])

    result = await _call_recall("oslo")
    # Must not raise, must return a result
    assert "content" in result
    text = _text(result)
    assert result["data"]["source"] == "legacy"
    assert "oslo" in text


# ---------------------------------------------------------------------------
# Test 6 — backfill skips when graph_backfill_done == '1'
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_backfill_skips_when_already_done(tmp_path: Path, monkeypatch):
    from storage import db

    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()

    db.runtime_set("graph_backfill_done", "1")

    add_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("storage.graph.add_episode_safe", add_mock)

    from scripts.backfill_facts_to_graph import main
    rc = await main()
    assert rc == 0
    add_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Test 7 — backfill idempotency: marks done after successful run
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_backfill_idempotency_after_success(tmp_path: Path, monkeypatch):
    from storage import db

    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()

    # Seed 3 facts into the temporary DB.
    for i in range(3):
        db.fact_insert(text=f"user fact {i}", source="user")

    add_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("storage.graph.add_episode_safe", add_mock)

    # Need to reload the module so it picks up the new db env
    import scripts.backfill_facts_to_graph as bf_mod
    importlib.reload(bf_mod)
    rc = await bf_mod.main()

    assert rc == 0
    assert add_mock.call_count == 3
    assert db.runtime_get("graph_backfill_done") == "1"
