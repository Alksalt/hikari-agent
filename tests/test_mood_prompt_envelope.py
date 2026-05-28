"""Phase 1 Scope B — mood prompt envelope + comfort-override arbitration.

Covers:
  1. cycle_state with warmth_multiplier=0.45 → "warmth_multiplier" and "low-tolerance"
     appear in the _format_core_blocks output.
  2. cycle_state with warmth_multiplier=1.3 → band "open" appears.
  3. comfort flag active + mood_today="irritable" → _format_mode_flags emits
     the comfort block with the override clause; does NOT emit an irritable barb-license.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari_envelope_test.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from agents import config
    config.reload()
    yield
    config.reload()


def _hooks():
    from agents import hooks
    return hooks


def _db():
    from storage import db
    return db


# ---------------------------------------------------------------------------
# 1. Low-tolerance band (warmth_multiplier < 0.6)
# ---------------------------------------------------------------------------

def test_core_blocks_injects_warmth_multiplier_low_tolerance():
    db = _db()
    db.upsert_core_block("cycle_state", json.dumps({
        "warmth_multiplier": 0.45,
        "cycle_phase": "low-tolerance",
        "composite_label": "inward / winter / low / night",
        "daily_phase": "night",
    }))
    hooks = _hooks()
    block = hooks._format_core_blocks()
    assert "warmth_multiplier" in block
    assert "low-tolerance" in block


# ---------------------------------------------------------------------------
# 2. Open band (warmth_multiplier >= 1.2)
# ---------------------------------------------------------------------------

def test_core_blocks_injects_warmth_multiplier_open():
    db = _db()
    db.upsert_core_block("cycle_state", json.dumps({
        "warmth_multiplier": 1.3,
        "cycle_phase": "peak-social",
        "composite_label": "peak-social / summer / high / afternoon",
        "daily_phase": "afternoon",
    }))
    hooks = _hooks()
    block = hooks._format_core_blocks()
    assert "warmth_multiplier" in block
    assert "open" in block


# ---------------------------------------------------------------------------
# 3. Comfort flag overrides irritable cycle mood
# ---------------------------------------------------------------------------

def test_format_mode_flags_comfort_overrides_irritable_cycle():
    db = _db()
    db.upsert_core_block("mood_today", "irritable")
    from agents import mode_dispatch
    mode_dispatch.activate_comfort_mode(trigger="they said something heavy")
    hooks = _hooks()
    result = hooks._format_mode_flags()
    assert "comfort mode" in result
    assert "overrides today's cycle mood" in result
    assert "anger mode" not in result
    assert "doubled down rude" not in result
