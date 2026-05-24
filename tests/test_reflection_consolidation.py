"""Daily consolidation pass — surviving behavior tests.

Covers:
  - Consolidation failures don't roll back the rest of reflection.

LLM calls are stubbed via ``monkeypatch`` so the tests are deterministic
and don't need network / API keys.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from agents import config, reflection
from storage import db


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    config.reload()
    yield


@pytest.mark.asyncio
async def test_consolidation_failure_does_not_break_reflection(monkeypatch):
    """If consolidation throws, the rest of reflection's writes must remain.

    We mock the LLM call AND the consolidation helper to raise; the
    reflection should still complete and return the True signal because
    the other extractions wrote rows.
    """
    # Seed at least one episode + fact so the reflection has something to
    # work with.
    db.insert_episode("2026-05-19", "stand-up")

    # Stub the reflection LLM to return a YAML doc that triggers writes.
    async def fake_run_reflection_call(_prompt):
        return (
            "new_facts:\n"
            "  - {subject: 'user', predicate: 'works_at', object: 'lab', "
            "importance: 7, confidence: 0.9}\n"
            "thought: |\n"
            "  this is fine.\n"
        )

    monkeypatch.setattr(reflection, "run_reflection_call", fake_run_reflection_call)

    # Make the consolidation step raise on entry.
    async def boom():
        raise RuntimeError("boom — consolidation explodes")

    monkeypatch.setattr(reflection, "_consolidate_yesterday", boom)

    # Embedding too — skip the model load.
    async def noop_embed(_fact_id, _s, _p, _o):
        return None

    monkeypatch.setattr(reflection, "_embed_fact", noop_embed)

    # Skip morning dispatch (touches the wiki path which doesn't exist in tests).
    monkeypatch.setattr(reflection, "_write_morning_dispatch",
                        lambda *a, **k: None)

    result = await reflection.run_daily_reflection()
    assert result is True
    # The fact landed.
    active = db.active_facts_matching("user", "works_at")
    assert len(active) == 1
    assert active[0]["object"] == "lab"
