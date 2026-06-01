"""tests/test_vec_search.py — unit tests for storage/db.py:vec_search_active_facts.

Test matrix:
  1. Happy path: inserts fact + embedding → search returns that fact id
  2. status='active' pre-filter: facts with valid_to IS NOT NULL are excluded
  3. Superseded fact (valid_to set): excluded from results
  4. Wrong embedding dimension → returns []
  5. Empty query vec → returns []
  6. vec_search (generic) raises on unsupported table name
"""
from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unit_vec() -> list[float]:
    """384-dim unit vector for deterministic KNN matches."""
    from storage.db import EMBEDDING_DIM
    return [1.0 / EMBEDDING_DIM] * EMBEDDING_DIM


def _zero_vec() -> list[float]:
    from storage.db import EMBEDDING_DIM
    return [0.0] * EMBEDDING_DIM


def _near_vec(offset: float = 0.0001) -> list[float]:
    """Slightly different from unit vec — still close enough for a hit."""
    from storage.db import EMBEDDING_DIM
    base = 1.0 / EMBEDDING_DIM
    return [base + offset] * EMBEDDING_DIM


# ---------------------------------------------------------------------------
# 1. Happy path: returns ranked results
# ---------------------------------------------------------------------------

def test_vec_search_active_facts_happy_path():
    from storage import db

    fid = db.insert_fact(subject="alice", predicate="likes", object_="dogs")
    db.set_vec_fact(fid, _unit_vec())

    results = db.vec_search_active_facts(_unit_vec(), k=5)
    assert len(results) >= 1
    ids = [r["id"] for r in results]
    assert fid in ids


# ---------------------------------------------------------------------------
# 2. status='active' filter: fact with valid_to IS NOT NULL is excluded
# ---------------------------------------------------------------------------

def test_vec_search_excludes_expired_fact():
    from storage import db

    # Insert an active fact and an expired fact (valid_to in the past)
    active_id = db.insert_fact(subject="user", predicate="likes", object_="cats")
    db.set_vec_fact(active_id, _unit_vec())

    expired_id = db.insert_fact(subject="user", predicate="hates", object_="mornings")
    db.set_vec_fact(expired_id, _unit_vec())

    # Manually set valid_to to past timestamp on expired fact
    with db._conn() as conn:
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        conn.execute(
            "UPDATE facts SET valid_to = ?, status = 'superseded' WHERE id = ?",
            (past, expired_id),
        )

    results = db.vec_search_active_facts(_unit_vec(), k=10)
    ids = [r["id"] for r in results]

    assert active_id in ids, "active fact should appear in results"
    assert expired_id not in ids, "expired/superseded fact must be excluded"


# ---------------------------------------------------------------------------
# 3. Superseded fact (valid_to set) excluded
# ---------------------------------------------------------------------------

def test_vec_search_excludes_superseded_fact():
    from storage import db

    kept_id = db.insert_fact(subject="user", predicate="knows", object_="python")
    db.set_vec_fact(kept_id, _unit_vec())

    superseded_id = db.insert_fact(subject="user", predicate="knows", object_="fortran")
    db.set_vec_fact(superseded_id, _unit_vec())

    # Mark as superseded with valid_to
    with db._conn() as conn:
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "UPDATE facts SET status = 'superseded', valid_to = ? WHERE id = ?",
            (now, superseded_id),
        )

    results = db.vec_search_active_facts(_unit_vec(), k=10)
    ids = [r["id"] for r in results]

    assert kept_id in ids
    assert superseded_id not in ids


# ---------------------------------------------------------------------------
# 4. Wrong embedding dimension → returns []
# ---------------------------------------------------------------------------

def test_vec_search_wrong_dim_returns_empty():
    from storage import db

    wrong_dim = [0.5] * 128  # wrong size
    results = db.vec_search_active_facts(wrong_dim, k=5)
    assert results == []


# ---------------------------------------------------------------------------
# 5. Empty query vec → returns []
# ---------------------------------------------------------------------------

def test_vec_search_empty_vec_returns_empty():
    from storage import db

    results = db.vec_search_active_facts([], k=5)
    assert results == []


# ---------------------------------------------------------------------------
# 6. vec_search raises on unsupported table name
# ---------------------------------------------------------------------------

def test_vec_search_unsupported_table_raises():
    from storage import db

    with pytest.raises(ValueError, match="unsupported vec table"):
        db.vec_search("unknown_table", _unit_vec(), k=5)


# ---------------------------------------------------------------------------
# 7. Multiple active facts — all appear in results, sorted by distance
# ---------------------------------------------------------------------------

def test_vec_search_returns_multiple_active_facts():
    from storage import db

    ids = []
    for i in range(3):
        fid = db.insert_fact(subject="u", predicate="has", object_=f"item_{i}")
        db.set_vec_fact(fid, _unit_vec())
        ids.append(fid)

    results = db.vec_search_active_facts(_unit_vec(), k=10)
    result_ids = [r["id"] for r in results]

    for fid in ids:
        assert fid in result_ids


# ---------------------------------------------------------------------------
# 8. Fact with no embedding is not returned
# ---------------------------------------------------------------------------

def test_fact_without_embedding_not_returned():
    from storage import db

    with_emb = db.insert_fact(subject="a", predicate="b", object_="c")
    db.set_vec_fact(with_emb, _unit_vec())

    without_emb = db.insert_fact(subject="x", predicate="y", object_="z")
    # no set_vec_fact call

    results = db.vec_search_active_facts(_unit_vec(), k=10)
    ids = [r["id"] for r in results]

    assert with_emb in ids
    assert without_emb not in ids
