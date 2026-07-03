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


@pytest.mark.asyncio
async def test_reflection_non_dict_yaml_skips_extraction(monkeypatch):
    """A bare-string LLM reply is a valid YAML scalar (str), not a mapping.
    The guard must NOT crash on data.get(...): it retries the LLM once, then —
    on a second non-mapping reply — skips the LLM-derived extraction while still
    running mechanical maintenance (decoupled), and records a skip breadcrumb."""
    db.insert_episode("2026-05-19", "stand-up")

    calls = []

    async def fake_run_reflection_call(_prompt):
        calls.append(_prompt)
        return "just a sentence — not a yaml mapping"

    monkeypatch.setattr(reflection, "run_reflection_call", fake_run_reflection_call)
    # Must not raise AttributeError. Retries once, then records the skip.
    await reflection.run_daily_reflection()
    # >= 2: the main extraction call + one retry (the maintenance B-block may
    # reuse run_reflection_call for consolidation, hence not an exact count).
    assert len(calls) >= 2, "reflection should retry the LLM once on a non-mapping reply"
    assert db.runtime_get("last_reflection_skipped") is not None


@pytest.mark.asyncio
async def test_reflection_first_call_exception_still_runs_maintenance(monkeypatch):
    """If the initial run_reflection_call raises (SDK error, timeout, etc.)
    reflection must not bail out before maintenance — it should log, stamp
    the skip breadcrumb, and fall through to the mechanical maintenance block
    exactly like the YAML-parse-failure path does."""
    db.insert_episode("2026-05-19", "stand-up")

    async def raising_run_reflection_call(_prompt):
        raise RuntimeError("SDK call failed")

    monkeypatch.setattr(reflection, "run_reflection_call", raising_run_reflection_call)

    # Skip morning dispatch (touches the wiki path which doesn't exist in tests).
    monkeypatch.setattr(reflection, "_write_morning_dispatch",
                        lambda *a, **k: None)

    result = await reflection.run_daily_reflection()
    # Maintenance ran (nothing raised) and the skip breadcrumb was stamped.
    assert result is False  # no facts/thought/preoc/etc. were produced this cycle
    assert db.runtime_get("last_reflection_skipped") is not None
    # Mechanical maintenance still executed — episode pruning ran without error,
    # proven by the function returning normally instead of raising/bailing early.


def test_near_dup_cosine_threshold_reads_config_live(monkeypatch):
    """Must not be frozen at import time — a cockpit config reload should be
    reflected on the next read without a process restart."""
    monkeypatch.setattr(
        reflection.cfg, "get",
        lambda key, default=None: 0.5 if key == "reflection.near_dup_cosine_threshold" else default,
    )
    assert reflection._near_dup_cosine_threshold() == 0.5

    monkeypatch.setattr(
        reflection.cfg, "get",
        lambda key, default=None: 0.99 if key == "reflection.near_dup_cosine_threshold" else default,
    )
    assert reflection._near_dup_cosine_threshold() == 0.99
