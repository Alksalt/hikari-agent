"""Phase 7 — structured peer_representation: db round-trip, hook injection,
migration from legacy user_profile core_block, dialectic merge."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from agents import config, peer_model
from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    config.reload()
    yield


# ---------- db round-trip ----------

def test_get_peer_representation_returns_none_on_empty():
    assert db.get_peer_representation() is None


def test_upsert_then_get_round_trip():
    model = {
        "communication_style": "terse, lowercase, no exclamation marks",
        "values": ["honesty", "competence", "no small talk"],
        "domain_expertise": ["python", "ML"],
        "current_concerns": ["the cabbage thing"],
        "blindspots": ["sleep schedule"],
        "summary": "ol — pragmatic engineer who texts lowercase.",
    }
    db.upsert_peer_representation(model)
    loaded = db.get_peer_representation()
    assert loaded is not None
    assert loaded["communication_style"] == model["communication_style"]
    assert loaded["values"] == model["values"]
    assert loaded["summary"] == model["summary"]


def test_upsert_overwrites():
    db.upsert_peer_representation({"summary": "old"})
    db.upsert_peer_representation({"summary": "new"})
    loaded = db.get_peer_representation()
    assert loaded["summary"] == "new"


def test_upsert_rejects_non_dict():
    with pytest.raises(TypeError):
        db.upsert_peer_representation("not a dict")  # type: ignore[arg-type]


# ---------- migration from legacy user_profile ----------

def test_migration_seeds_from_user_profile_on_first_run():
    """If a legacy core_blocks.user_profile row exists and peer_representation
    is empty, _migrate_user_profile_to_peer_representation seeds it on every boot.

    This migration is a data-conditional seeding op (not a DDL migration) and is
    intentionally NOT wrapped in run_once — it checks on every _ensure_schema call
    and returns early if peer_representation already has a row."""
    db.upsert_core_block("user_profile", "Ol — Ukrainian, 29, lives in Norway.")
    # Reset the sentinel to force _ensure_schema to re-run the migration cascade.
    db._reset_schema_sentinel()
    with db._conn():
        pass
    loaded = db.get_peer_representation()
    assert loaded is not None
    assert "Ol" in loaded["summary"]


def test_migration_does_not_clobber_existing_peer_representation():
    """If peer_representation already has data, migration must NOT overwrite it."""
    db.upsert_peer_representation({"summary": "already-structured"})
    db.upsert_core_block("user_profile", "legacy content")
    db._reset_schema_sentinel()
    with db._conn():
        pass
    loaded = db.get_peer_representation()
    assert loaded["summary"] == "already-structured"


def test_migration_skips_when_no_user_profile_block():
    """No legacy data → no peer_representation row created."""
    db._reset_schema_sentinel()
    with db._conn():
        pass
    assert db.get_peer_representation() is None


def test_migration_idempotent_across_many_connections():
    """Stress idempotency — calling _conn() many times after a successful
    seed must not crash or duplicate the single-row peer_representation."""
    db.upsert_core_block("user_profile", "stress content")
    db._reset_schema_sentinel()
    for _ in range(15):
        with db._conn():
            pass
    # The single-row CHECK (id = 1) would error on a second INSERT — surviving
    # 15 connection cycles proves the early-return guard works.
    loaded = db.get_peer_representation()
    assert loaded is not None
    assert "stress content" in loaded["summary"]


# ---------- format_for_injection ----------

def test_format_for_injection_empty_model_returns_empty_string():
    assert peer_model.format_for_injection(None) == ""
    assert peer_model.format_for_injection({}) == ""


def test_format_for_injection_renders_populated_model():
    model = {
        "communication_style": "terse",
        "values": ["honesty"],
        "domain_expertise": ["python"],
        "current_concerns": ["the cabbage thing"],
        "blindspots": ["sleep"],
        "summary": "pragmatic engineer",
    }
    out = peer_model.format_for_injection(model)
    assert "pragmatic engineer" in out
    assert "terse" in out
    assert "honesty" in out
    assert "python" in out
    assert "cabbage thing" in out
    assert "sleep" in out
    assert out.startswith("# memory: who they are")


def test_format_for_injection_skips_empty_fields():
    model = {"summary": "just a summary"}
    out = peer_model.format_for_injection(model)
    assert "just a summary" in out
    # No "they're competent at" line should appear since domain_expertise is empty.
    assert "competent at" not in out


# ---------- dialectic merge ----------

def test_merge_preserves_old_and_unions_new_lists():
    old = {
        "communication_style": "terse",
        "values": ["honesty"],
        "domain_expertise": ["python"],
        "current_concerns": [],
        "blindspots": [],
        "summary": "old summary",
    }
    new = {
        "values": ["competence"],  # new entry
        "domain_expertise": ["python", "ML"],  # overlap + new
        "current_concerns": ["cabbage thing"],
    }
    merged = peer_model.merge_dialectic(old, new)
    assert merged["communication_style"] == "terse"  # preserved
    assert "honesty" in merged["values"] and "competence" in merged["values"]
    assert merged["domain_expertise"] == ["python", "ML"]  # dedup
    assert merged["current_concerns"] == ["cabbage thing"]
    assert merged["summary"] == "old summary"  # not overwritten by empty


def test_merge_overwrites_summary_on_non_empty_new():
    old = {"summary": "old"}
    merged = peer_model.merge_dialectic(old, {"summary": "fresh"})
    assert merged["summary"] == "fresh"


def test_merge_caps_lists_at_ten():
    old = {"values": [f"v{i}" for i in range(8)]}
    new = {"values": [f"v{i}" for i in range(20, 25)]}
    merged = peer_model.merge_dialectic(old, new)
    assert len(merged["values"]) == 10
    # Oldest pruned, newest kept.
    assert "v20" in merged["values"]


def test_merge_empty_old_returns_normalized_new():
    merged = peer_model.merge_dialectic(None, {"summary": "hello"})
    assert merged["summary"] == "hello"
    assert merged["values"] == []


def test_merge_handles_garbage_new_observations():
    """Non-dict input or bad types should not crash."""
    merged = peer_model.merge_dialectic({"summary": "x"}, "not a dict")  # type: ignore
    assert merged["summary"] == "x"
    merged = peer_model.merge_dialectic({"summary": "x"}, None)
    assert merged["summary"] == "x"


# ---------- hook injection respects user_profile filter ----------

def test_hook_format_core_blocks_filters_user_profile():
    """Even if a legacy user_profile core_block exists, _format_core_blocks
    must NOT include it (the peer_representation handles that content now)."""
    from agents import hooks
    db.upsert_core_block("user_profile", "should be hidden")
    db.upsert_core_block("mood_today", "tired")
    out = hooks._format_core_blocks()
    assert "tired" in out  # mood survives
    assert "should be hidden" not in out  # user_profile filtered


def test_hook_format_peer_representation_uses_db():
    from agents import hooks
    db.upsert_peer_representation({"summary": "structured summary content"})
    out = hooks._format_peer_representation()
    assert "structured summary content" in out
