"""Tests for accountability_items schema and DB helpers."""
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


def _make_reminder(minutes_from_now: int = 60) -> int:
    fire = (datetime.now(UTC) + timedelta(minutes=minutes_from_now)).isoformat()
    return db.reminder_insert(fire_at=fire, text="test")


def test_table_exists():
    with db._conn() as conn:
        tables = {
            row[0] for row in
            conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert "accountability_items" in tables


def test_fk_rejects_orphan_reminder_id():
    import sqlite3
    with db._conn() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO accountability_items "
                "(reminder_id, follow_up_reminder_id, task_text) "
                "VALUES (9999, 9998, 'orphan')"
            )


def test_indexes_exist():
    with db._conn() as conn:
        idx_names = {
            row[0] for row in
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='accountability_items'"
            ).fetchall()
        }
    assert "idx_accountability_followup" in idx_names
    assert "idx_accountability_unresolved" in idx_names


def test_insert_and_get():
    rid = _make_reminder(60)
    frid = _make_reminder(120)
    item_id = db.accountability_insert(rid, frid, "drink water")
    assert item_id > 0
    item = db.accountability_get(item_id)
    assert item is not None
    assert item["task_text"] == "drink water"
    assert item["outcome"] is None
    assert item["resolved_at"] is None


def test_get_by_followup_id():
    rid = _make_reminder(60)
    frid = _make_reminder(120)
    item_id = db.accountability_insert(rid, frid, "exercise")
    found = db.accountability_get_by_followup_id(frid)
    assert found is not None
    assert found["id"] == item_id
    assert found["task_text"] == "exercise"


def test_get_by_followup_id_missing():
    assert db.accountability_get_by_followup_id(9999) is None


def test_resolve_did():
    rid = _make_reminder(60)
    frid = _make_reminder(120)
    item_id = db.accountability_insert(rid, frid, "meditate")
    db.accountability_resolve(item_id, 1)
    item = db.accountability_get(item_id)
    assert item["outcome"] == 1
    assert item["resolved_at"] is not None


def test_resolve_didnt():
    rid = _make_reminder(60)
    frid = _make_reminder(120)
    item_id = db.accountability_insert(rid, frid, "run")
    db.accountability_resolve(item_id, 0)
    item = db.accountability_get(item_id)
    assert item["outcome"] == 0
    assert item["resolved_at"] is not None


def test_recent_unresolved():
    rid1 = _make_reminder(60)
    frid1 = _make_reminder(120)
    id1 = db.accountability_insert(rid1, frid1, "task A")

    rid2 = _make_reminder(180)
    frid2 = _make_reminder(240)
    id2 = db.accountability_insert(rid2, frid2, "task B")

    # Resolve one.
    db.accountability_resolve(id1, 1)

    unresolved = db.accountability_recent_unresolved(limit=5)
    ids = [u["id"] for u in unresolved]
    assert id2 in ids
    assert id1 not in ids


def test_accountability_create_atomic_rolls_back_on_failure(monkeypatch):
    """If the third INSERT (accountability_items) fails, the two reminder
    inserts must roll back — no partial state."""
    import sqlite3
    from contextlib import contextmanager

    before_reminders = db.reminder_list(active_only=False)
    before_count = len(before_reminders)

    orig_conn = db._conn

    @contextmanager
    def failing_conn():
        with orig_conn() as conn:
            class _FailingConn:
                _calls = 0

                def execute(self, sql, *args, **kw):
                    if "INSERT INTO accountability_items" in (sql if isinstance(sql, str) else ""):
                        raise sqlite3.OperationalError("simulated failure")
                    return conn.execute(sql, *args, **kw)

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return conn.__exit__(*a)

            yield _FailingConn()

    monkeypatch.setattr(db, "_conn", failing_conn)

    primary_iso = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    follow_iso = (datetime.now(UTC) + timedelta(hours=5)).isoformat()
    try:
        db.accountability_create_atomic(primary_iso, follow_iso, "test task")
        assert False, "should have raised"
    except sqlite3.OperationalError:
        pass

    monkeypatch.undo()

    after_reminders = db.reminder_list(active_only=False)
    after_count = len(after_reminders)
    assert before_count == after_count, "first two reminder inserts must roll back"

    with db._conn() as c:
        n_test = c.execute(
            "SELECT COUNT(*) FROM accountability_items WHERE task_text = ?",
            ("test task",)
        ).fetchone()[0]
    assert n_test == 0, "no accountability_items row for this task should exist"


def test_stats_round_trip():
    rid1 = _make_reminder(10)
    frid1 = _make_reminder(20)
    id1 = db.accountability_insert(rid1, frid1, "a")
    db.accountability_resolve(id1, 1)

    rid2 = _make_reminder(30)
    frid2 = _make_reminder(40)
    id2 = db.accountability_insert(rid2, frid2, "b")
    db.accountability_resolve(id2, 0)

    rid3 = _make_reminder(50)
    frid3 = _make_reminder(60)
    db.accountability_insert(rid3, frid3, "c")  # unresolved

    stats = db.accountability_stats()
    assert stats["total"] == 3
    assert stats["resolved"] == 2
    assert stats["did"] == 1
    assert stats["didnt"] == 1
    assert abs(stats["did_rate"] - 0.5) < 0.001
