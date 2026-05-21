"""Bi-temporal fact_relations: edges get valid_to + invalidated_by_fact_id
when a fact is superseded. Recall must filter invalidated edges out."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    yield


def test_migration_adds_columns_idempotently():
    from storage import db
    # Trigger schema.
    db.upsert_core_block("ping", "ping")
    with db._conn() as c:
        cols = {r["name"] for r in
                c.execute("PRAGMA table_info(fact_relations)").fetchall()}
    assert "valid_to" in cols
    assert "invalidated_by_fact_id" in cols


def test_invalidate_stamps_edges_for_superseded_fact():
    from storage import db
    f1 = db.insert_fact("user", "lives in", "kyiv", importance=9)
    f2 = db.insert_fact("user", "works at", "acme", importance=8)
    f3 = db.insert_fact("user", "has cat named", "nori", importance=7)
    db.fact_relation_insert(f1, "co_occurs_with", f2)
    db.fact_relation_insert(f2, "co_occurs_with", f3)
    db.fact_relation_insert(f1, "co_occurs_with", f3)

    f1_v2 = db.insert_fact("user", "lives in", "lisbon", importance=9)
    db.mark_fact_invalid(f1, superseded_by=f1_v2)

    with db._conn() as c:
        rows = c.execute(
            "SELECT id, valid_to, invalidated_by_fact_id "
            "FROM fact_relations "
            "WHERE subject_fact_id = ? OR object_fact_id = ?",
            (f1, f1),
        ).fetchall()
    assert all(r["valid_to"] is not None for r in rows), (
        "all edges touching f1 should be invalidated"
    )
    assert all(r["invalidated_by_fact_id"] == f1_v2 for r in rows)
    # The unrelated edge (f2 -> f3) is untouched.
    with db._conn() as c:
        unrelated = c.execute(
            "SELECT valid_to FROM fact_relations "
            "WHERE subject_fact_id = ? AND object_fact_id = ?",
            (f2, f3),
        ).fetchone()
    assert unrelated["valid_to"] is None


def test_recall_filters_invalidated_edges():
    """fact_relations_for(fact_id) must skip valid_to IS NOT NULL rows."""
    from storage import db
    f1 = db.insert_fact("user", "works at", "acme", importance=9)
    f2 = db.insert_fact("user", "drinks", "coffee", importance=5)
    db.fact_relation_insert(f1, "co_occurs_with", f2)

    f1_v2 = db.insert_fact("user", "works at", "globex", importance=9)
    db.mark_fact_invalid(f1, superseded_by=f1_v2)

    rels = db.fact_relations_for(f1)
    assert rels == [], "invalidated edges must be filtered from recall"


def test_invalidate_helper_is_idempotent():
    from storage import db
    f1 = db.insert_fact("user", "note", "x", importance=5)
    f2 = db.insert_fact("user", "note", "y", importance=5)
    db.fact_relation_insert(f1, "co_occurs_with", f2)
    f3 = db.insert_fact("user", "note", "z", importance=5)
    n1 = db.fact_relations_invalidate_for_fact(f1, invalidated_by=f3)
    n2 = db.fact_relations_invalidate_for_fact(f1, invalidated_by=f3)
    assert n1 == 1
    assert n2 == 0  # already invalidated, no double-stamp
