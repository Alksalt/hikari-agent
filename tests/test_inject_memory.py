"""Sprint A: inject_memory integration tests for new blocks.

Covers:
  1. deferred_observations block appears in additionalContext.
  2. peer_insights block appears in additionalContext when rows exist.
  3. emotional_register block appears in additionalContext when set.
  4. composite_label appears in additionalContext when cycle_state set.
  5. stage hint appears in additionalContext when relationship_stage set.
  6. time_texture appears in the # now block in additionalContext.
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
    db_path = tmp_path / "hikari_inject_memory_test.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from agents import config
    config.reload()
    yield
    config.reload()


def _call(user_prompt: str = "hi") -> str:
    from agents.hooks import inject_memory
    result = asyncio.run(inject_memory({"prompt": user_prompt}, None, None))
    return result.get("hookSpecificOutput", {}).get("additionalContext", "")


def _db():
    from storage import db
    return db


def test_deferred_observations_in_context():
    db = _db()
    payload = json.dumps({
        "text": "you seemed off earlier — just noticed",
        "created_at": datetime.now(UTC).isoformat(),
    })
    db.runtime_set("deferred_observations", payload)
    ctx = _call()
    assert "deferred observation" in ctx
    assert "seemed off" in ctx


def test_peer_insights_in_context():
    db = _db()
    db.peer_insight_insert("brings up sleep issues late at night", surface_score=0.9)
    ctx = _call()
    assert "noticed patterns" in ctx
    assert "sleep" in ctx


def test_emotional_register_in_context():
    db = _db()
    with db._conn() as conn:
        try:
            conn.execute("ALTER TABLE session ADD COLUMN emotional_register TEXT")
        except Exception:
            pass
        conn.execute(
            "INSERT INTO session (id, emotional_register) VALUES (1, 'tense') "
            "ON CONFLICT(id) DO UPDATE SET emotional_register = 'tense'"
        )
    ctx = _call()
    assert "emotional register" in ctx
    assert "tense" in ctx


def test_composite_label_in_context():
    db = _db()
    db.upsert_core_block("cycle_state", json.dumps({
        "composite_label": "warmth_dip",
        "warmth_multiplier": 0.8,
    }))
    db.upsert_core_block("mood_today", "irritable")
    ctx = _call()
    assert "composite_label: warmth_dip" in ctx


def test_stage_hint_in_context():
    db = _db()
    db.upsert_core_block("relationship_stage", "4")
    db.upsert_core_block("mood_today", "focused")
    ctx = _call()
    assert "stage 4" in ctx
    assert "compliment 1/20" in ctx


def test_time_texture_in_now_block():
    db = _db()
    db.runtime_set("time_texture", "late_night")
    ctx = _call()
    assert "time_texture: late_night" in ctx


# ---------------------------------------------------------------------------
# _select_blocks budget — priority-1 (always-on core) must not starve the
# optional priority-2/3 relational-texture blocks. Regression: the old loop
# folded the ~8.5k-char core into the same counter as the 4096 cap, so every
# texture block was silently dropped on every turn ("feels dumb").
# ---------------------------------------------------------------------------

def test_select_blocks_priority1_does_not_starve_optional():
    from agents.hooks import _Block, _select_blocks
    candidates = [
        _Block(key="core_big", priority=1, order=0, text="X" * 9000),  # huge always-on core
        _Block(key="texture_a", priority=2, order=1, text="A" * 100),
        _Block(key="texture_b", priority=3, order=2, text="B" * 100),
    ]
    selected, _ = _select_blocks(candidates, max_chars=4096, sep="\n\n")
    keys = {b.key for b in selected}
    assert "core_big" in keys, "priority-1 core is always included"
    assert "texture_a" in keys, "priority-2 texture must not be starved by the core footprint"
    assert "texture_b" in keys, "priority-3 texture must not be starved by the core footprint"


def test_select_blocks_optional_budget_still_enforced():
    from agents.hooks import _Block, _select_blocks
    candidates = [
        _Block(key="core", priority=1, order=0, text="C" * 50),
        _Block(key="t1", priority=2, order=1, text="A" * 3000),
        _Block(key="t2", priority=2, order=2, text="B" * 3000),  # together exceed 4096
    ]
    selected, _ = _select_blocks(candidates, max_chars=4096, sep="\n\n")
    keys = {b.key for b in selected}
    assert "core" in keys
    assert "t1" in keys
    assert "t2" not in keys, "priority-2/3 are still capped by max_chars among themselves"
