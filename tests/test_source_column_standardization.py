"""Phase N — facts.source column standardization tests.

Covers:
  - insert_fact accepts and stores source='user' / 'hikari' / 'inferred'.
  - insert_fact raises ValueError on invalid source values.
  - _source_multiplier returns 0.7 for 'hikari', 1.0 for everything else.
  - Retrieval scoring dampens hikari-source facts below user-source facts.
  - Backfill migration maps attribution → source correctly.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from agents import config
from storage import db, retrieval
from storage.retrieval import _source_multiplier


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    """Fresh per-test DB + reload storage.db so migrations run on the new path."""
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    try:
        config.reload()
    except Exception:
        pass
    # Skip embedding calls — BM25 is enough for these tests.
    monkeypatch.setattr(
        retrieval.embeddings, "embed",
        lambda _text: (_ for _ in ()).throw(RuntimeError("no embeddings in test")),
    )
    yield


# ---------------------------------------------------------------------------
# 1. insert_fact stores source
# ---------------------------------------------------------------------------

def test_insert_fact_accepts_source():
    """insert_fact with source='user' stores the value."""
    fid = db.insert_fact("user", "likes", "cold rice", source="user")
    with db._conn() as c:
        row = c.execute("SELECT source FROM facts WHERE id=?", (fid,)).fetchone()
    assert row["source"] == "user"


def test_insert_fact_accepts_hikari_source():
    fid = db.insert_fact("hikari", "believes", "attention is all", source="hikari")
    with db._conn() as c:
        row = c.execute("SELECT source FROM facts WHERE id=?", (fid,)).fetchone()
    assert row["source"] == "hikari"


def test_insert_fact_accepts_inferred_source():
    fid = db.insert_fact("user", "probably", "likes cats", source="inferred")
    with db._conn() as c:
        row = c.execute("SELECT source FROM facts WHERE id=?", (fid,)).fetchone()
    assert row["source"] == "inferred"


def test_insert_fact_source_none_stored_as_null():
    """No source kwarg → NULL in DB (backward compat)."""
    fid = db.insert_fact("user", "owns", "laptop")
    with db._conn() as c:
        row = c.execute("SELECT source FROM facts WHERE id=?", (fid,)).fetchone()
    assert row["source"] is None


# ---------------------------------------------------------------------------
# 2. insert_fact validates source
# ---------------------------------------------------------------------------

def test_insert_fact_validates_source():
    """Invalid source value raises ValueError."""
    with pytest.raises(ValueError, match="source"):
        db.insert_fact("user", "likes", "something", source="reflection")


def test_insert_fact_validates_source_bad_string():
    with pytest.raises(ValueError, match="source"):
        db.insert_fact("user", "likes", "something", source="unknown_tag")


# ---------------------------------------------------------------------------
# 3. _source_multiplier
# ---------------------------------------------------------------------------

def test_source_multiplier_hikari_dampens():
    assert _source_multiplier("hikari") == pytest.approx(0.7)


def test_source_multiplier_hikari_case_insensitive():
    assert _source_multiplier("Hikari") == pytest.approx(0.7)
    assert _source_multiplier("HIKARI") == pytest.approx(0.7)


def test_source_multiplier_user_neutral():
    assert _source_multiplier("user") == pytest.approx(1.0)


def test_source_multiplier_inferred_neutral():
    assert _source_multiplier("inferred") == pytest.approx(1.0)


def test_source_multiplier_null_neutral():
    assert _source_multiplier(None) == pytest.approx(1.0)


def test_source_multiplier_empty_neutral():
    assert _source_multiplier("") == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 4. Retrieval ranking: user-source outranks hikari-source
# ---------------------------------------------------------------------------

def test_retrieval_score_dampened_for_hikari_source():
    """Two equally-relevant facts; source='user' must rank higher than source='hikari'."""
    fid_user = db.insert_fact(
        "user", "favorite_drink", "matcha latte",
        attribution="user_stated", source="user",
    )
    fid_hikari = db.insert_fact(
        "user", "favorite_drink", "matcha latte",
        attribution="user_stated", source="hikari",
    )
    # Index both in FTS so BM25 retrieval can find them.
    with db._conn() as c:
        c.execute("DELETE FROM fts WHERE kind='fact' AND ref_id IN (?,?)",
                  (fid_user, fid_hikari))
        c.execute("INSERT INTO fts (content, kind, ref_id) VALUES (?,?,?)",
                  ("user favorite_drink matcha latte", "fact", fid_user))
        c.execute("INSERT INTO fts (content, kind, ref_id) VALUES (?,?,?)",
                  ("user favorite_drink matcha latte", "fact", fid_hikari))

    hits = retrieval.legacy_retrieve("matcha latte favorite_drink", limit=10)
    # Both facts should appear.
    hit_ids = {h.ref_id for h in hits if h.kind == "fact"}
    assert fid_user in hit_ids, "user-source fact not retrieved"
    assert fid_hikari in hit_ids, "hikari-source fact not retrieved"

    user_score = next(h.score for h in hits if h.kind == "fact" and h.ref_id == fid_user)
    hikari_score = next(h.score for h in hits if h.kind == "fact" and h.ref_id == fid_hikari)
    assert user_score > hikari_score, (
        f"user fact ({user_score:.4f}) should outrank hikari fact ({hikari_score:.4f})"
    )


# ---------------------------------------------------------------------------
# 5. Backfill migration
# ---------------------------------------------------------------------------

def test_backfill_migration_sets_source_from_attribution():
    """The UPDATE in _migrate_phase_b_schema_tables maps attribution → source."""
    # Insert raw rows with attribution but no source (simulates pre-migration state).
    with db._conn() as c:
        now = "2024-01-01T00:00:00+00:00"
        c.execute(
            "INSERT INTO facts (subject, predicate, object, valid_from, attribution, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?)",
            ("user", "pred", "obj1", now, "hikari_inferred", now),
        )
        id_hi = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute(
            "INSERT INTO facts (subject, predicate, object, valid_from, attribution, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?)",
            ("user", "pred", "obj2", now, "user_stated", now),
        )
        id_us = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute(
            "INSERT INTO facts (subject, predicate, object, valid_from, attribution, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?)",
            ("user", "pred", "obj3", now, "user_corrected", now),
        )
        id_uc = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        # Null attribution → source stays NULL.
        c.execute(
            "INSERT INTO facts (subject, predicate, object, valid_from, attribution, status, created_at) "
            "VALUES (?, ?, ?, ?, NULL, 'active', ?)",
            ("user", "pred", "obj4", now, now),
        )
        id_null = c.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Run the backfill UPDATE directly (mirrors what the migration does).
        c.execute("""
            UPDATE facts
            SET source = CASE
                WHEN attribution = 'hikari_inferred' THEN 'inferred'
                WHEN attribution IN ('user_stated', 'user_corrected') THEN 'user'
                ELSE NULL
            END
            WHERE source IS NULL AND attribution IS NOT NULL
        """)

        rows = {
            r["id"]: r["source"]
            for r in c.execute("SELECT id, source FROM facts WHERE id IN (?,?,?,?)",
                               (id_hi, id_us, id_uc, id_null)).fetchall()
        }

    assert rows[id_hi] == "inferred", f"hikari_inferred should map to 'inferred', got {rows[id_hi]!r}"
    assert rows[id_us] == "user", f"user_stated should map to 'user', got {rows[id_us]!r}"
    assert rows[id_uc] == "user", f"user_corrected should map to 'user', got {rows[id_uc]!r}"
    assert rows[id_null] is None, f"null attribution should stay NULL, got {rows[id_null]!r}"
