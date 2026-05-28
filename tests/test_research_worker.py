"""Tests for agents.subagents.research_worker."""
from __future__ import annotations

import asyncio
import importlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _setup(monkeypatch, tmp_path):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    from storage import db
    importlib.reload(db)
    db._reset_schema_sentinel()
    return db


def _seed_task(db, subject="look into X", description="context", status="pending",
               research_intent=1, created_at=None):
    """Insert a task row directly into the DB, respecting Phase O columns."""
    if created_at is None:
        from datetime import UTC, datetime
        created_at = datetime.now(UTC).isoformat()
    with db._conn() as c:
        cur = c.execute(
            "INSERT INTO tasks (subject, description, status, research_intent, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (subject, description, status, research_intent, created_at),
        )
    return cur.lastrowid


# ---------------------------------------------------------------------------
# test_skips_when_disabled
# ---------------------------------------------------------------------------

def test_skips_when_disabled(monkeypatch, tmp_path):
    db = _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))

    with patch("agents.config.get", return_value=False):
        from agents.subagents import research_worker
        importlib.reload(research_worker)
        result = asyncio.run(research_worker.run_research_worker())
    assert result == 0


# ---------------------------------------------------------------------------
# test_skips_when_daily_cap_reached
# ---------------------------------------------------------------------------

def test_skips_when_daily_cap_reached(monkeypatch, tmp_path):
    db = _setup(monkeypatch, tmp_path)
    _seed_task(db)

    from agents.subagents import research_worker
    importlib.reload(research_worker)

    # Simulate cap already reached.
    from datetime import date
    db.runtime_set("research_worker.loops_today_date", date.today().isoformat())
    db.runtime_set("research_worker.loops_today", 2)  # max_loops_per_day default = 2

    result = asyncio.run(research_worker.run_research_worker())
    assert result == 0


# ---------------------------------------------------------------------------
# test_skips_when_lock_held
# ---------------------------------------------------------------------------

def test_skips_when_lock_held(monkeypatch, tmp_path):
    db = _setup(monkeypatch, tmp_path)
    _seed_task(db)

    from agents.subagents import research_worker
    importlib.reload(research_worker)

    fake_lock = MagicMock()
    fake_lock.locked.return_value = True
    with patch("agents.runtime._RUN_LOCK", fake_lock):
        result = asyncio.run(research_worker.run_research_worker())
    assert result == 0


# ---------------------------------------------------------------------------
# test_processes_pending_task_writes_summary
# ---------------------------------------------------------------------------

def test_processes_pending_task_writes_summary(monkeypatch, tmp_path):
    db = _setup(monkeypatch, tmp_path)
    task_id = _seed_task(db)

    from agents.subagents import research_worker
    importlib.reload(research_worker)

    # Mock the SDK session to return a summary.
    async def fake_research_one(task):
        return ("This is a research summary. https://example.com/src", ["https://example.com/src"])

    with patch.object(research_worker, "_research_one", side_effect=fake_research_one):
        result = asyncio.run(research_worker.run_research_worker())

    assert result == 1
    with db._conn() as c:
        row = c.execute("SELECT research_summary, research_sources_json, research_attempted_at FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row is not None
    assert "research summary" in row["research_summary"]
    assert row["research_sources_json"] is not None
    assert row["research_attempted_at"] is not None


# ---------------------------------------------------------------------------
# test_marks_attempted_on_empty_result
# ---------------------------------------------------------------------------

def test_marks_attempted_on_empty_result(monkeypatch, tmp_path):
    db = _setup(monkeypatch, tmp_path)
    task_id = _seed_task(db)

    from agents.subagents import research_worker
    importlib.reload(research_worker)

    async def fake_research_one(task):
        return None

    with patch.object(research_worker, "_research_one", side_effect=fake_research_one):
        result = asyncio.run(research_worker.run_research_worker())

    assert result == 0
    with db._conn() as c:
        row = c.execute("SELECT research_summary, research_attempted_at FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["research_summary"] == "(no useful sources)"
    assert row["research_attempted_at"] is not None


# ---------------------------------------------------------------------------
# test_filters_old_tasks
# ---------------------------------------------------------------------------

def test_filters_old_tasks(monkeypatch, tmp_path):
    db = _setup(monkeypatch, tmp_path)
    from datetime import UTC, datetime, timedelta
    old_ts = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    _seed_task(db, created_at=old_ts)

    from agents.subagents import research_worker
    importlib.reload(research_worker)

    async def fake_research_one(task):
        return ("summary", [])

    with patch.object(research_worker, "_research_one", side_effect=fake_research_one):
        result = asyncio.run(research_worker.run_research_worker())

    # task_age_max_days default = 14, task is 30 days old → skipped
    assert result == 0
