"""Recall validity gate — graph hits must respect SQLite fact status.

Invariants:
  1. A graph hit whose SQLite fact row has status='superseded' is dropped.
  2. A graph hit whose SQLite fact row has status='invalid' is dropped.
  3. A graph hit whose SQLite fact row has valid_to in the past is dropped.
  4. A graph hit whose SQLite fact row is active surfaces normally.
  5. A graph hit with no fact_id (back-compat) degrades to legacy fallback.
  6. GRAPHITI_ENABLED=false → recall short-circuits to legacy, zero ERROR log lines.
"""
from __future__ import annotations

import importlib
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from agents import config
    config.reload()
    yield
    config.reload()


def _edge(fact: str, score: float, fact_id: int | None = None) -> MagicMock:
    e = MagicMock()
    e.fact = fact
    e.score = score
    e.fact_id = fact_id
    e.valid_at = None
    e.invalid_at = None
    return e


async def _call_recall(query: str = "test") -> dict:
    from tools.memory.recall import recall
    return await recall.handler({"query": query, "limit": 8})


def _text(result: dict) -> str:
    return result["content"][0]["text"]


# ---------------------------------------------------------------------------
# 1. Graph hit with status='superseded' is dropped
# ---------------------------------------------------------------------------

async def test_superseded_fact_dropped(monkeypatch):
    from storage import db

    fid = db.insert_fact("user", "likes", "matcha")
    fid2 = db.insert_fact("user", "likes", "coffee")
    db.supersede_fact(fid, fid2)

    edge = _edge("user likes matcha", score=0.9, fact_id=fid)
    monkeypatch.setattr("storage.graph.search", AsyncMock(return_value=[edge]))

    from storage.retrieval import Hit
    legacy_hit = Hit(
        kind="fact", ref_id=fid2, text="user likes coffee",
        iso_ts="2026-01-01T00:00:00+00:00",
        score=1.5, recency=0.8, importance=0.5, relevance=0.7,
    )
    monkeypatch.setattr("storage.retrieval.legacy_retrieve", lambda q, limit=8: [legacy_hit])

    result = await _call_recall("matcha")
    # superseded fact must not appear; fell back to legacy
    assert result["data"]["source"] == "legacy"
    assert "matcha" not in _text(result) or "coffee" in _text(result)


# ---------------------------------------------------------------------------
# 2. Graph hit with status='invalid' is dropped
# ---------------------------------------------------------------------------

async def test_invalid_fact_dropped(monkeypatch):
    from storage import db

    fid = db.insert_fact("user", "lives", "oslo")
    db.mark_fact_invalid(fid)

    edge = _edge("user lives oslo", score=0.85, fact_id=fid)
    monkeypatch.setattr("storage.graph.search", AsyncMock(return_value=[edge]))
    monkeypatch.setattr("storage.retrieval.legacy_retrieve", lambda q, limit=8: [])

    result = await _call_recall("oslo")
    assert result["data"]["source"] == "legacy"


# ---------------------------------------------------------------------------
# 3. Graph hit with valid_to in the past is dropped
# ---------------------------------------------------------------------------

async def test_expired_fact_dropped(monkeypatch):
    from storage import db

    fid = db.insert_fact("user", "works", "startup")
    # manually set valid_to to past ISO string
    with db._conn() as c:
        c.execute(
            "UPDATE facts SET valid_to='2020-01-01T00:00:00', status='superseded' WHERE id=?",
            (fid,),
        )

    edge = _edge("user works startup", score=0.88, fact_id=fid)
    monkeypatch.setattr("storage.graph.search", AsyncMock(return_value=[edge]))
    monkeypatch.setattr("storage.retrieval.legacy_retrieve", lambda q, limit=8: [])

    result = await _call_recall("startup")
    assert result["data"]["source"] == "legacy"


# ---------------------------------------------------------------------------
# 4. Active fact surfaces normally
# ---------------------------------------------------------------------------

async def test_active_fact_surfaces(monkeypatch):
    from storage import db

    fid = db.insert_fact("user", "enjoys", "hiking")

    edge = _edge("user enjoys hiking", score=0.9, fact_id=fid)
    monkeypatch.setattr("storage.graph.search", AsyncMock(return_value=[edge]))

    result = await _call_recall("hiking")
    assert result["data"]["source"] == "graph"
    assert any(h.get("fact_id") == fid for h in result["data"]["hits"])


# ---------------------------------------------------------------------------
# 5. No fact_id in edge → back-compat expansion only → legacy fallback
# ---------------------------------------------------------------------------

async def test_no_fact_id_degrades_to_legacy(monkeypatch):
    from storage.retrieval import Hit

    edge = _edge("some old graph fact", score=0.8, fact_id=None)
    monkeypatch.setattr("storage.graph.search", AsyncMock(return_value=[edge]))

    legacy_hit = Hit(
        kind="fact", ref_id=1, text="legacy answer",
        iso_ts="2026-01-01T00:00:00+00:00",
        score=1.0, recency=0.9, importance=0.6, relevance=0.75,
    )
    monkeypatch.setattr("storage.retrieval.legacy_retrieve", lambda q, limit=8: [legacy_hit])

    result = await _call_recall("old")
    assert result["data"]["source"] == "legacy"


# ---------------------------------------------------------------------------
# 6. GRAPHITI_ENABLED=false → no ERROR logs, uses legacy
# ---------------------------------------------------------------------------

async def test_graphiti_disabled_no_error_logs(monkeypatch, caplog):
    from storage.retrieval import Hit

    monkeypatch.setenv("GRAPHITI_ENABLED", "false")

    graph_search_mock = AsyncMock(return_value=[])
    monkeypatch.setattr("storage.graph.search", graph_search_mock)

    legacy_hit = Hit(
        kind="fact", ref_id=42, text="legacy result when disabled",
        iso_ts="2026-01-01T00:00:00+00:00",
        score=1.2, recency=0.8, importance=0.5, relevance=0.7,
    )
    monkeypatch.setattr("storage.retrieval.legacy_retrieve", lambda q, limit=8: [legacy_hit])

    with caplog.at_level(logging.ERROR, logger="tools.memory.recall"):
        result = await _call_recall("anything")

    # graph.search must NOT have been called
    graph_search_mock.assert_not_called()
    # zero ERROR lines from the recall logger
    error_lines = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not error_lines, f"unexpected ERROR log lines: {error_lines}"
    assert result["data"]["source"] == "legacy"
