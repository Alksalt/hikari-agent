"""T3.1 — bi-temporal facts.

Covers:
  - mark_fact_invalid() sets valid_to + status='invalid' when no replacement
  - mark_fact_invalid(superseded_by=...) sets status='superseded' and the new
    superseded_by_fact_id pointer; the old row is preserved (history kept)
  - recall (via storage.retrieval) excludes invalidated facts even when their
    FTS row is left in place
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from agents import config
from storage import db


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    """Per-test sqlite DB so migrations + writes don't bleed across tests."""
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    config.reload()
    yield


def _read_fact(fact_id: int) -> dict:
    with db._conn() as c:
        row = c.execute(
            "SELECT * FROM facts WHERE id = ?", (fact_id,)
        ).fetchone()
    assert row is not None, f"fact #{fact_id} not found"
    return dict(row)


def test_mark_fact_invalid_sets_valid_to_and_status():
    """No-replacement invalidation: status flips to 'invalid', valid_to is stamped."""
    fid = db.fact_insert(text="lives in Oslo", source="user_message")
    db.mark_fact_invalid(fid)
    row = _read_fact(fid)
    assert row["status"] == "invalid"
    assert row["valid_to"] is not None
    # No replacement was provided, so the supersede pointer must stay NULL.
    assert row["superseded_by_fact_id"] is None


def test_mark_fact_superseded_keeps_history():
    """Replacement path: status='superseded', pointer set, both rows still exist."""
    old = db.fact_insert(text="lives in Oslo", source="user_message")
    new = db.fact_insert(text="lives in Kristiansund", source="user_message")
    db.mark_fact_invalid(old, superseded_by=new)
    row = _read_fact(old)
    assert row["status"] == "superseded"
    assert row["superseded_by_fact_id"] == new
    assert row["valid_to"] is not None
    # The new fact stays active.
    new_row = _read_fact(new)
    assert new_row["status"] == "active"
    assert new_row["valid_to"] is None


def test_recall_excludes_invalid_facts(monkeypatch):
    """retrieve() must not return facts whose valid_to is in the past."""
    from storage import retrieval

    # Skip the actual embedding call (model load is slow + offline-hostile);
    # BM25 alone proves the filter works at the SQL/hydrate layer.
    monkeypatch.setattr(
        retrieval.embeddings, "embed",
        lambda _text: (_ for _ in ()).throw(RuntimeError("no embeddings in test")),
    )

    fid = db.fact_insert(text="loves cabbage stew", source="user_message")
    # Sanity: active fact is retrievable.
    hits = retrieval.retrieve("cabbage stew", limit=5)
    assert any(h.kind == "fact" and h.ref_id == fid for h in hits), (
        f"expected fid={fid} in hits, got {[(h.kind, h.ref_id) for h in hits]}"
    )

    # Invalidate and re-query.
    db.mark_fact_invalid(fid)
    hits_after = retrieval.retrieve("cabbage stew", limit=5)
    assert not any(h.kind == "fact" and h.ref_id == fid for h in hits_after), (
        f"invalidated fid={fid} leaked into hits: "
        f"{[(h.kind, h.ref_id) for h in hits_after]}"
    )


def test_fact_insert_round_trip_columns():
    """The new text/source-shaped insert populates the bi-temporal columns
    so downstream readers can rely on them."""
    fid = db.fact_insert(text="hates rainy mornings", source="user_message")
    row = _read_fact(fid)
    assert row["status"] == "active"
    assert row["valid_from"] is not None
    assert row["valid_to"] is None
    assert row["source"] == "user_message"
    assert row["superseded_by_fact_id"] is None


def test_legacy_supersede_fact_also_sets_status():
    """The existing supersede_fact() helper must populate status='superseded'
    + superseded_by_fact_id so callers that haven't migrated still produce
    bi-temporal rows."""
    old = db.insert_fact("user", "lives_in", "Oslo")
    new = db.insert_fact("user", "lives_in", "Kristiansund")
    db.supersede_fact(old, new, reason="moved")
    row = _read_fact(old)
    assert row["status"] == "superseded"
    assert row["superseded_by_fact_id"] == new
    assert row["valid_to"] is not None


def test_legacy_invalidate_fact_sets_status_invalid():
    fid = db.insert_fact("user", "likes", "warm rice")
    db.invalidate_fact(fid, reason="just a test")
    row = _read_fact(fid)
    assert row["status"] == "invalid"
    assert row["valid_to"] is not None
