"""Sprint A: hooks.py additions.

Covers:
  1. _format_now includes time_texture when set.
  2. _format_core_blocks injects composite_label from cycle_state.
  3. _format_core_blocks injects stage hint from relationship_stage.
  4. _format_core_blocks injects world line from hikari_world + hikari_currently_into.
  5. _format_peer_insights renders unsurfaced rows and marks them surfaced.
  6. _format_emotional_register renders the session emotional_register column.
  7. _format_deferred_observations clears the slot after injection.
  8. inject_memory writes last_user_message before calling render functions.
"""
from __future__ import annotations

import asyncio
import importlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari_hooks_test.db"
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
# 1. time_texture in # now block
# ---------------------------------------------------------------------------

def test_format_now_includes_time_texture_when_set():
    db = _db()
    db.runtime_set("time_texture", "morning")
    hooks = _hooks()
    block = hooks._format_now()
    assert "time_texture: morning" in block


def test_format_now_omits_time_texture_when_absent():
    hooks = _hooks()
    block = hooks._format_now()
    assert "time_texture" not in block


# ---------------------------------------------------------------------------
# 2. composite_label injected from cycle_state
# ---------------------------------------------------------------------------

def test_core_blocks_injects_composite_label():
    db = _db()
    db.upsert_core_block("cycle_state", json.dumps({
        "composite_label": "warmth_peak",
        "warmth_multiplier": 1.2,
    }))
    db.upsert_core_block("mood_today", "focused")
    hooks = _hooks()
    block = hooks._format_core_blocks()
    assert "composite_label: warmth_peak" in block


def test_core_blocks_no_crash_with_invalid_cycle_state():
    db = _db()
    db.upsert_core_block("cycle_state", "not-valid-json")
    db.upsert_core_block("mood_today", "focused")
    hooks = _hooks()
    block = hooks._format_core_blocks()
    assert "mood_today" in block


# ---------------------------------------------------------------------------
# 3. Stage gate hint
# ---------------------------------------------------------------------------

def test_core_blocks_injects_stage_hint_stage_3():
    db = _db()
    db.upsert_core_block("relationship_stage", "3")
    db.upsert_core_block("mood_today", "tired")
    hooks = _hooks()
    block = hooks._format_core_blocks()
    assert "stage 3" in block
    assert "compliment 1/30" in block


def test_core_blocks_injects_stage_hint_stage_7():
    db = _db()
    db.upsert_core_block("relationship_stage", "7")
    db.upsert_core_block("mood_today", "tired")
    hooks = _hooks()
    block = hooks._format_core_blocks()
    assert "stage 7" in block
    assert "i love you" in block


def test_core_blocks_no_stage_hint_when_absent():
    db = _db()
    db.upsert_core_block("mood_today", "focused")
    hooks = _hooks()
    block = hooks._format_core_blocks()
    assert "stage " not in block


# ---------------------------------------------------------------------------
# 4. hikari_world / hikari_currently_into line
# ---------------------------------------------------------------------------

def test_core_blocks_injects_world_line():
    db = _db()
    db.upsert_core_block("hikari_world", json.dumps({"location": "home", "activity": "reading"}))
    db.upsert_core_block("hikari_currently_into", json.dumps(["neural ODEs", "Arrival OST"]))
    db.upsert_core_block("mood_today", "focused")
    hooks = _hooks()
    block = hooks._format_core_blocks()
    assert "world:" in block


# ---------------------------------------------------------------------------
# 5. peer_insights block
# ---------------------------------------------------------------------------

def test_format_peer_insights_renders_and_marks_surfaced():
    db = _db()
    db.peer_insight_insert("often mentions sleep issues at night", surface_score=0.8)
    hooks = _hooks()
    block = hooks._format_peer_insights()
    assert "noticed patterns" in block
    assert "sleep" in block
    # After injection, the row should be marked surfaced
    remaining = db.peer_insights_unsurfaced(limit=10)
    assert len(remaining) == 0


def test_format_peer_insights_empty_when_no_rows():
    hooks = _hooks()
    block = hooks._format_peer_insights()
    assert block == ""


# ---------------------------------------------------------------------------
# 6. emotional register
# ---------------------------------------------------------------------------

def test_format_emotional_register_renders_when_set():
    db = _db()
    with db._conn() as conn:
        # Make sure the column exists (migration may not have run on fresh DB)
        try:
            conn.execute(
                "ALTER TABLE session ADD COLUMN emotional_register TEXT"
            )
        except Exception:
            pass
        conn.execute(
            "INSERT INTO session (id, emotional_register) VALUES (1, 'warm') "
            "ON CONFLICT(id) DO UPDATE SET emotional_register = 'warm'"
        )
    hooks = _hooks()
    block = hooks._format_emotional_register()
    assert "emotional register" in block
    assert "warm" in block


def test_format_emotional_register_empty_when_null():
    hooks = _hooks()
    block = hooks._format_emotional_register()
    assert block == ""


# ---------------------------------------------------------------------------
# 7. deferred_observations cleared after injection
# ---------------------------------------------------------------------------

def test_deferred_observations_injected_and_cleared():
    db = _db()
    payload = json.dumps({
        "text": "you seemed off earlier — not diagnosing, just noticed",
        "created_at": datetime.now(UTC).isoformat(),
    })
    db.runtime_set("deferred_observations", payload)
    hooks = _hooks()
    block = hooks._format_deferred_observations()
    assert block is not None
    assert "deferred observation" in block
    assert "seemed off" in block
    # Slot must be cleared
    assert db.runtime_get("deferred_observations") is None


def test_deferred_observations_expired_ttl_returns_none():
    db = _db()
    old_ts = "2020-01-01T00:00:00+00:00"
    payload = json.dumps({
        "text": "stale observation",
        "created_at": old_ts,
    })
    db.runtime_set("deferred_observations", payload)
    hooks = _hooks()
    block = hooks._format_deferred_observations()
    assert block is None
    assert db.runtime_get("deferred_observations") is None


def test_deferred_observations_empty_when_absent():
    hooks = _hooks()
    block = hooks._format_deferred_observations()
    assert block is None


# ---------------------------------------------------------------------------
# 8. last_user_message written at hook entry (before LLM call)
# ---------------------------------------------------------------------------

def test_inject_memory_writes_last_user_message():
    db = _db()
    db.upsert_core_block("mood_today", "focused")
    # Start with no last_user_message
    assert db.runtime_get("last_user_message") is None
    from agents.hooks import inject_memory
    asyncio.run(inject_memory({"prompt": "hello"}, None, None))
    # After the hook fires, last_user_message must be set
    assert db.runtime_get("last_user_message") is not None
