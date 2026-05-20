"""Phase 11: weekly sleep-time consolidation.

Letta sleep-time pattern (Apr 2025): live agent + sleep agent. The sleep
agent runs while the user is idle, synthesizes a 200-word "what i noticed
about him this week" summary into ``core_blocks['weekly_consolidation']``.
Previous week's content is archived to ``weekly_consolidations_archive``
before being overwritten.

These tests stub the LLM call so they're deterministic and offline.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agents import config
from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    config.reload()
    yield


@pytest.mark.asyncio
async def test_weekly_consolidation_writes_core_block(monkeypatch):
    """Happy path: thoughts in the last 7 days → LLM call → core_block set."""
    # Insert some character_thoughts spanning the last 7 days. NB: actual
    # column is ``thought`` (not ``thought_text``).
    with db._conn() as conn:
        for i in range(5):
            day = (datetime.now(UTC) - timedelta(days=i)).isoformat()
            conn.execute(
                "INSERT INTO character_thoughts (thought, created_at) VALUES (?, ?)",
                (f"thought {i}", day),
            )

    # Mock the reflection call to avoid hitting the SDK.
    from agents import reflection

    async def fake_call(prompt, **kwargs):
        return "she noticed he was tired three times this week. she didn't say anything."

    monkeypatch.setattr(reflection, "run_reflection_call", fake_call)

    from agents.reflection import run_weekly_consolidation
    result = await run_weekly_consolidation()
    assert result is True

    block = db.get_core_block("weekly_consolidation")
    assert block is not None
    assert "tired" in block


@pytest.mark.asyncio
async def test_weekly_consolidation_archives_previous(monkeypatch):
    """Existing weekly_consolidation core_block is archived before overwrite."""
    # Seed an existing weekly_consolidation block.
    db.upsert_core_block("weekly_consolidation", "old summary from last week")

    # Insert a thought so consolidation actually runs (non-empty window).
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO character_thoughts (thought, created_at) VALUES (?, ?)",
            ("recent thought", datetime.now(UTC).isoformat()),
        )

    from agents import reflection

    async def fake_call(prompt, **kwargs):
        return "new summary"

    monkeypatch.setattr(reflection, "run_reflection_call", fake_call)

    from agents.reflection import run_weekly_consolidation
    ok = await run_weekly_consolidation()
    assert ok is True

    # Old summary is archived.
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT summary_text FROM weekly_consolidations_archive"
        ).fetchall()
    assert any("old summary" in r[0] for r in rows)

    # New summary is the current core_block.
    assert db.get_core_block("weekly_consolidation") == "new summary"


@pytest.mark.asyncio
async def test_weekly_consolidation_handles_empty_week():
    """No data in any of the source tables → returns False, no core_block, no
    LLM call."""
    from agents.reflection import run_weekly_consolidation
    result = await run_weekly_consolidation()
    assert result is False
    assert db.get_core_block("weekly_consolidation") is None
    # And nothing got archived since there was nothing to archive.
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM weekly_consolidations_archive"
        ).fetchone()
    assert int(rows["n"]) == 0


@pytest.mark.asyncio
async def test_weekly_consolidation_no_archive_when_no_prior(monkeypatch):
    """First run ever — no existing core_block, so nothing should be archived
    but the new summary still lands."""
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO character_thoughts (thought, created_at) VALUES (?, ?)",
            ("first ever thought", datetime.now(UTC).isoformat()),
        )

    from agents import reflection

    async def fake_call(prompt, **kwargs):
        return "first week summary"

    monkeypatch.setattr(reflection, "run_reflection_call", fake_call)

    from agents.reflection import run_weekly_consolidation
    assert await run_weekly_consolidation() is True

    with db._conn() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM weekly_consolidations_archive"
        ).fetchone()
    assert int(rows["n"]) == 0
    assert db.get_core_block("weekly_consolidation") == "first week summary"


@pytest.mark.asyncio
async def test_weekly_consolidation_llm_failure_returns_false(monkeypatch):
    """If the LLM call raises, return False — daily reflection that may be
    running in parallel must not be affected."""
    with db._conn() as conn:
        conn.execute(
            "INSERT INTO character_thoughts (thought, created_at) VALUES (?, ?)",
            ("a thought", datetime.now(UTC).isoformat()),
        )

    from agents import reflection

    async def boom(prompt, **kwargs):
        raise RuntimeError("LLM exploded")

    monkeypatch.setattr(reflection, "run_reflection_call", boom)

    from agents.reflection import run_weekly_consolidation
    assert await run_weekly_consolidation() is False
    assert db.get_core_block("weekly_consolidation") is None


def test_weekly_consolidation_insert_helper_roundtrip():
    """Helper writes a row + reader returns it."""
    rid = db.weekly_consolidation_insert(
        week_ending="2026-05-17",
        summary_text="last week — quiet, mostly code",
        episode_count=4,
    )
    assert rid > 0
    rows = db.weekly_consolidations_recent(limit=10)
    assert len(rows) == 1
    item = rows[0]
    assert item["week_ending"] == "2026-05-17"
    assert "quiet" in item["summary_text"]
    assert int(item["episode_count"]) == 4


def test_weekly_consolidation_insert_rejects_empty():
    with pytest.raises(ValueError):
        db.weekly_consolidation_insert(
            week_ending="", summary_text="x", episode_count=1
        )
    with pytest.raises(ValueError):
        db.weekly_consolidation_insert(
            week_ending="2026-05-17", summary_text="", episode_count=1
        )
