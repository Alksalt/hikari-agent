"""Phase 8 (control-plane-lies sweep) — /memory correct must embed the
corrected fact so it's findable by semantic recall.

Before the fix, correct_fact inserted the new (highest-trust) fact but never
wrote a vector, so the corrected value was invisible to KNN recall — the worst
possible outcome for the most authoritative version of a fact.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    _db_mod._reset_schema_sentinel()
    _db_mod.get_session_id()
    from agents import config
    config.reload()
    yield


def _vec_fact_exists(db, fact_id: int) -> bool:
    with db._conn() as c:
        row = c.execute("SELECT id FROM vec_facts WHERE id = ?", (fact_id,)).fetchone()
    return row is not None


def test_correct_fact_embeds_the_corrected_fact(monkeypatch):
    """The new fact gets a vector (mirrors remember.py) → recall can find it."""
    import storage.db as db
    from tools import embeddings
    from tools.memory.correct_fact import correct_fact

    # Deterministic embedding — avoids loading the real fastembed model.
    monkeypatch.setattr(embeddings, "embed", lambda text: [0.1] * embeddings.EMBEDDING_DIM)

    old_id = db.insert_fact("user", "drinks", "coffee", attribution="user_stated", source="user")
    new_id = correct_fact(old_id, "tea")

    assert new_id != old_id
    assert _vec_fact_exists(db, new_id), (
        "corrected fact must have a vector embedding — without it the "
        "highest-trust fact is invisible to semantic recall"
    )
    assert db.fact_by_id(new_id)["object"] == "tea"


def test_correct_fact_survives_embedding_failure(monkeypatch):
    """If embedding fails, the correction still completes (degraded recall,
    not a lost correction) — embedding is enhancement, the correction is the
    critical operation."""
    import storage.db as db
    from tools import embeddings
    from tools.memory.correct_fact import correct_fact

    def _boom(_text):
        raise RuntimeError("embedder unavailable")

    monkeypatch.setattr(embeddings, "embed", _boom)

    old_id = db.insert_fact("user", "drinks", "coffee", attribution="user_stated", source="user")
    new_id = correct_fact(old_id, "tea")  # must NOT raise

    assert db.fact_by_id(new_id)["object"] == "tea"
    assert not _vec_fact_exists(db, new_id)  # no vector this time, but fact corrected


# ---------------------------------------------------------------------------
# A3: _infer_category regression tests
# ---------------------------------------------------------------------------

class TestInferCategory:
    """_infer_category maps predicates onto the exact TAU_BY_CATEGORY keys."""

    def test_like_infers_preference(self):
        from storage.retrieval import _infer_category
        assert _infer_category("likes") == "preference"

    def test_love_infers_preference(self):
        from storage.retrieval import _infer_category
        assert _infer_category("loves cooking") == "preference"

    def test_went_infers_event(self):
        from storage.retrieval import _infer_category
        assert _infer_category("went to") == "event"

    def test_bought_infers_event(self):
        from storage.retrieval import _infer_category
        assert _infer_category("bought a car") == "event"

    def test_generic_defaults_to_fact(self):
        from storage.retrieval import _infer_category
        assert _infer_category("has_pet") == "fact"

    def test_case_insensitive(self):
        from storage.retrieval import _infer_category
        assert _infer_category("LIKES") == "preference"
        assert _infer_category("Went") == "event"

    def test_correct_fact_carries_old_category(self, monkeypatch):
        """correct_fact must preserve the original fact_category row value."""
        import storage.db as db
        from tools import embeddings
        from tools.memory.correct_fact import correct_fact

        monkeypatch.setattr(embeddings, "embed", lambda text: [0.1] * embeddings.EMBEDDING_DIM)

        old_id = db.insert_fact(
            "user", "likes", "jazz",
            attribution="user_stated", source="user",
            fact_category="preference",
        )
        new_id = correct_fact(old_id, "blues")

        new_row = db.fact_by_id(new_id)
        assert new_row["fact_category"] == "preference", (
            "correct_fact must carry the original fact_category so ACT-R "
            "uses the same tau after a correction"
        )

    def test_correct_fact_carries_none_when_column_absent(self, monkeypatch):
        """Old facts without a fact_category (pre-column migration) must not crash."""
        import storage.db as db
        from tools import embeddings
        from tools.memory.correct_fact import correct_fact

        monkeypatch.setattr(embeddings, "embed", lambda text: [0.1] * embeddings.EMBEDDING_DIM)

        old_id = db.insert_fact(
            "user", "drinks", "water",
            attribution="user_stated", source="user",
            # fact_category intentionally omitted → NULL in DB
        )
        new_id = correct_fact(old_id, "sparkling water")  # must not raise

        assert db.fact_by_id(new_id)["object"] == "sparkling water"
