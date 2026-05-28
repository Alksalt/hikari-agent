"""Phase L — SelfRepresentation TypedDict, merge helpers, format_self_for_injection,
and their_model_of_me field on PeerRepresentation."""

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


# ---------- SelfRepresentation defaults ----------

def test_self_representation_typed_dict_defaults():
    s = peer_model.empty_self()
    assert s["current_voice_register"] == ""
    assert s["recent_deflection_rate"] == 0.0
    assert s["mood_prediction_accuracy"] == 0.0
    assert s["drift_vectors"] == []
    assert s["last_updated_iso"] == ""


# ---------- merge_self_dialectic ----------

def test_merge_self_dialectic_caps_drift_vectors_at_5():
    old = peer_model.empty_self()
    old["drift_vectors"] = ["a", "b", "c"]
    new = {"drift_vectors": ["d", "e", "f"]}
    merged = peer_model.merge_self_dialectic(old, new)
    assert len(merged["drift_vectors"]) == 5
    # last 5 of combined ["a","b","c","d","e","f"]
    assert merged["drift_vectors"] == ["b", "c", "d", "e", "f"]


def test_merge_self_dialectic_overwrites_voice_register():
    old = {"current_voice_register": "dry/peak", "recent_deflection_rate": 0.7,
           "mood_prediction_accuracy": 0.0, "drift_vectors": [], "last_updated_iso": ""}
    new = {"current_voice_register": "soft/inward"}
    merged = peer_model.merge_self_dialectic(old, new)
    assert merged["current_voice_register"] == "soft/inward"
    # existing float preserved
    assert merged["recent_deflection_rate"] == 0.7


def test_merge_self_dialectic_skips_zero_floats():
    """Zero float in new should NOT overwrite existing non-zero float."""
    old = {"current_voice_register": "", "recent_deflection_rate": 0.6,
           "mood_prediction_accuracy": 0.0, "drift_vectors": [], "last_updated_iso": ""}
    new = {"recent_deflection_rate": 0.0}  # zero — should be skipped
    merged = peer_model.merge_self_dialectic(old, new)
    assert merged["recent_deflection_rate"] == 0.6


def test_merge_self_dialectic_none_old_returns_new():
    new = {"current_voice_register": "terse", "recent_deflection_rate": 0.4}
    merged = peer_model.merge_self_dialectic(None, new)
    assert merged["current_voice_register"] == "terse"
    assert merged["recent_deflection_rate"] == 0.4


# ---------- PeerRepresentation their_model_of_me ----------

def test_peer_representation_their_model_of_me_default_empty():
    e = peer_model.empty()
    assert "their_model_of_me" in e
    assert e["their_model_of_me"] == {}


def test_peer_merge_dialectic_preserves_their_model_of_me():
    old = peer_model.empty()
    old["their_model_of_me"] = {"beliefs_about_hikari": ["she cares"]}
    new_obs = {"their_model_of_me": {"expected_responses": ["will deflect"]}}
    merged = peer_model.merge_dialectic(old, new_obs)
    tmom = merged["their_model_of_me"]
    assert tmom["beliefs_about_hikari"] == ["she cares"]  # old preserved
    assert tmom["expected_responses"] == ["will deflect"]   # new added


# ---------- format_self_for_injection ----------

def test_format_self_for_injection_returns_empty_when_all_empty():
    assert peer_model.format_self_for_injection(None) == ""
    assert peer_model.format_self_for_injection({}) == ""
    assert peer_model.format_self_for_injection(peer_model.empty_self()) == ""


def test_format_self_for_injection_includes_voice_and_drift():
    model = {
        "current_voice_register": "dry/peak",
        "recent_deflection_rate": 0.75,
        "mood_prediction_accuracy": 0.0,
        "drift_vectors": ["warmer than usual", "over-explaining"],
        "last_updated_iso": "",
    }
    out = peer_model.format_self_for_injection(model)
    assert out.startswith("# self-model")
    assert "dry/peak" in out
    assert "0.75" in out
    assert "warmer than usual" in out


def test_format_self_for_injection_trims_drift_to_last_3():
    model = {
        "current_voice_register": "",
        "recent_deflection_rate": 0.0,
        "mood_prediction_accuracy": 0.0,
        "drift_vectors": ["a", "b", "c", "d", "e"],
        "last_updated_iso": "",
    }
    out = peer_model.format_self_for_injection(model)
    # should show last 3
    assert "c" in out or "d" in out or "e" in out
    assert "a" not in out


# ---------- db round-trip for self_representation ----------

def test_get_self_representation_returns_none_on_empty_seed():
    # The seed row is '{}' — the helper returns None for empty dicts.
    assert db.get_self_representation() is None


def test_upsert_then_get_self_representation():
    content = {"current_voice_register": "dry/peak", "recent_deflection_rate": 0.6,
               "mood_prediction_accuracy": 0.0, "drift_vectors": ["over-explaining"],
               "last_updated_iso": "2026-05-28T00:00:00+00:00"}
    db.upsert_self_representation(content)
    loaded = db.get_self_representation()
    assert loaded is not None
    assert loaded["current_voice_register"] == "dry/peak"
    assert loaded["recent_deflection_rate"] == 0.6


def test_upsert_self_representation_rejects_non_dict():
    with pytest.raises(TypeError):
        db.upsert_self_representation("not a dict")  # type: ignore[arg-type]
