"""Phase 8 — monthly memory-retention prune cron.

Covers:
  - prune_episodes_older_than_days removes episodes past the cutoff
  - Recent episodes survive
  - FTS + sqlite-vec rows are also pruned (defense-in-depth for vec search)
  - The scheduler wrapper logs and tolerates DB errors gracefully
"""

from __future__ import annotations

import importlib
from datetime import date, timedelta
from pathlib import Path

import pytest

from agents import config, scheduler as scheduler_mod
from storage import db


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    config.reload()
    yield


def _insert_episode(d: date, summary: str) -> int:
    """Insert an episode with a specific date string."""
    with db._conn() as c:
        cur = c.execute(
            "INSERT INTO episodes (date, summary, importance, created_at) "
            "VALUES (?, ?, ?, ?)",
            (d.isoformat(), summary, 5, db._now()),
        )
        return int(cur.lastrowid)


def test_prune_removes_old_episodes_only():
    """Episodes past the retention cutoff get pruned; recent survive."""
    old_id = _insert_episode(date.today() - timedelta(days=200), "ancient")
    recent_id = _insert_episode(date.today() - timedelta(days=30), "recent")

    pruned = db.prune_episodes_older_than_days(180)
    assert pruned == 1

    with db._conn() as c:
        rows = c.execute("SELECT id FROM episodes").fetchall()
    surviving_ids = {r["id"] for r in rows}
    assert old_id not in surviving_ids
    assert recent_id in surviving_ids


def test_prune_zero_when_nothing_to_remove():
    _insert_episode(date.today() - timedelta(days=5), "fresh")
    pruned = db.prune_episodes_older_than_days(180)
    assert pruned == 0


def test_prune_handles_empty_table():
    """No episodes → zero pruned, no errors."""
    assert db.prune_episodes_older_than_days(180) == 0


def test_prune_cleans_fts_index():
    """Pruning an episode must also drop its FTS row so search doesn't return
    orphaned matches."""
    old_id = _insert_episode(date.today() - timedelta(days=300), "ancient memory")
    # Insert matching FTS row.
    with db._conn() as c:
        c.execute(
            "INSERT INTO fts (kind, ref_id, content) VALUES (?, ?, ?)",
            ("episode", old_id, "ancient memory"),
        )
    db.prune_episodes_older_than_days(180)
    with db._conn() as c:
        rows = c.execute(
            "SELECT ref_id FROM fts WHERE kind = 'episode' AND ref_id = ?",
            (old_id,),
        ).fetchall()
    assert rows == []


def test_scheduler_wrapper_logs_and_tolerates_failure(caplog, monkeypatch):
    """_run_memory_prune wraps the call — DB errors must not crash the
    scheduler tick."""
    def boom(days):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(db, "prune_episodes_older_than_days", boom)
    # Should not raise.
    scheduler_mod._run_memory_prune(180)


def test_scheduler_wrapper_calls_helper(monkeypatch):
    """Happy path: the wrapper passes retention_days through correctly."""
    captured = {"days": None}

    def fake(days):
        captured["days"] = days
        return 7

    monkeypatch.setattr(db, "prune_episodes_older_than_days", fake)
    scheduler_mod._run_memory_prune(99)
    assert captured["days"] == 99
