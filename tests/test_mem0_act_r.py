"""Phase M: ACT-R activation + Mem0 entity-match fusion tests.

Covers:
  - Category-specific tau: event decays faster than fact for the same age.
  - Epsilon=0 produces deterministic results.
  - Epsilon>0 produces varied results.
  - Entity extraction from a query (integration with DB).
  - facts_for_entity_ids with empty input.
  - facts_for_entity_ids with a seeded link.
  - NULL category falls back to 29d (fact) tau.
  - _normalize_category from reflection.py: valid / invalid / empty inputs.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from storage import db, retrieval
from storage.retrieval import (
    _act_r_activation,
    _extract_query_entity_ids,
    _facts_for_entity_ids,
)

# ---------------------------------------------------------------------------
# Fixture: isolated DB per test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari_m.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    # Stub embeddings to avoid slow model loads.
    monkeypatch.setattr(
        retrieval.embeddings, "embed",
        lambda _text: (_ for _ in ()).throw(RuntimeError("no embeddings in test")),
    )
    yield


# ---------------------------------------------------------------------------
# ACT-R activation — pure-function tests (no DB)
# ---------------------------------------------------------------------------

def test_act_r_decay_category_specific_tau():
    """Same age, different category → event decays more than fact.

    At 5 days old with epsilon=0:
      - event tau = 3d  → t = 5/3 = 1.67 > 1 → t^-0.5 = 0.775
      - fact  tau = 29d → t = 5/29 = 0.17, clamped to 1.0 → t^-0.5 = 1.0
    base(event) = 0.775 < base(fact) = 1.0
    → A(event) = log(0.775) < A(fact) = log(1.0) = 0
    → exp(A(event)) < exp(A(fact)) = 1.0
    So event activation < fact activation (event decays faster).
    """
    age_sec = 5 * 86400.0  # 5 days
    event_act = _act_r_activation(age_sec, [], "event", epsilon=0.0)
    fact_act  = _act_r_activation(age_sec, [], "fact",  epsilon=0.0)
    assert event_act < fact_act, (
        f"event activation {event_act:.4f} should be < fact activation {fact_act:.4f}"
    )


def test_act_r_noise_disabled_when_epsilon_zero():
    """Same call with epsilon=0 → identical results across multiple invocations."""
    age_sec = 7 * 86400.0
    results = [
        _act_r_activation(age_sec, [], "fact", epsilon=0.0)
        for _ in range(5)
    ]
    assert all(r == results[0] for r in results), (
        f"expected identical results with epsilon=0, got {results}"
    )


def test_act_r_noise_varies_with_epsilon():
    """epsilon=0.5 → results differ across calls (not all equal)."""
    age_sec = 7 * 86400.0
    results = [
        _act_r_activation(age_sec, [], "fact", epsilon=0.5)
        for _ in range(20)
    ]
    assert len(set(f"{r:.6f}" for r in results)) > 1, (
        "expected varied results with epsilon=0.5, but all were identical"
    )


def test_null_category_defaults_to_fact_tau():
    """category=None must use TAU_DEFAULT_SECONDS (29d) — same as 'fact'."""
    age_sec = 7 * 86400.0
    none_act = _act_r_activation(age_sec, [], None,   epsilon=0.0)
    fact_act  = _act_r_activation(age_sec, [], "fact", epsilon=0.0)
    assert none_act == pytest.approx(fact_act, abs=1e-9), (
        f"None category {none_act} should match fact tau {fact_act}"
    )


# ---------------------------------------------------------------------------
# Entity extraction — DB-backed tests
# ---------------------------------------------------------------------------

def test_entity_match_extraction():
    """Query 'ship the Acme model' with Acme entity in DB → entity_id returned."""
    eid = db.entity_upsert("project", "Acme")
    result = _extract_query_entity_ids("ship the Acme model")
    assert eid in result, f"expected entity_id {eid} in {result}"


def test_facts_for_entity_ids_empty_input():
    """Empty entity_id set → empty result (no DB query attempted)."""
    result = _facts_for_entity_ids(set())
    assert result == set()


def test_facts_for_entity_ids_with_link():
    """Seed a fact + entity + link → fact_id returned."""
    eid = db.entity_upsert("person", "Alice")
    fid = db.insert_fact(
        subject="Alice", predicate="likes", object_="tea",
        source="user",
    )
    db.fact_entities_link(fid, [eid])
    result = _facts_for_entity_ids({eid})
    assert fid in result, f"expected fact_id {fid} in {result}"


# ---------------------------------------------------------------------------
# _normalize_category from reflection.py
# ---------------------------------------------------------------------------

def test_reflection_normalize_category_valid():
    from agents.reflection import _normalize_category
    assert _normalize_category("preference") == "preference"


def test_reflection_normalize_category_invalid_falls_back():
    from agents.reflection import _normalize_category
    assert _normalize_category("garbage") == "fact"


def test_reflection_normalize_category_empty_default():
    from agents.reflection import _normalize_category
    assert _normalize_category(None) == "fact"
    assert _normalize_category("") == "fact"
