"""Tests for the message, audit_log, and calendar_kv pruner functions."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield


def _insert_old_message(days_ago: int) -> int:
    with db._conn() as c:
        cur = c.execute(
            "INSERT INTO messages (role, content, ts) VALUES (?, ?, datetime('now', ? || ' days'))",
            ("user", "old message", f"-{days_ago}"),
        )
        return int(cur.lastrowid)


def _insert_recent_message() -> int:
    return db.append_message("user", "recent message")


def test_prune_messages_removes_old_keeps_recent():
    old_id = _insert_old_message(60)
    recent_id = _insert_recent_message()

    deleted = db.prune_messages_older_than_days(30)
    assert deleted == 1

    with db._conn() as c:
        ids = {r["id"] for r in c.execute("SELECT id FROM messages").fetchall()}
    assert old_id not in ids
    assert recent_id in ids


def test_prune_messages_empty_table():
    assert db.prune_messages_older_than_days(30) == 0


def test_prune_messages_all_recent_survives():
    _insert_recent_message()
    assert db.prune_messages_older_than_days(30) == 0


def test_audit_log_pruner_is_intentionally_absent():
    """The audit_log is a SHA-chained forensic ledger — pruning ANY row
    invalidates every subsequent row's hash_prev. The pruner was removed
    rather than ship something that silently breaks the chain. Treat the
    audit_log as append-only forever."""
    assert not hasattr(db, "prune_audit_log_older_than_days")


def test_calendar_notified_pruner_is_intentionally_absent():
    """runtime_state has no updated_at column; the only safe purge would be
    unconditional, which collides with the calendar heartbeat's own 4h TTL
    contract (agents/proactive.py). Deferred to a later sprint when a proper
    schema (e.g. a dedicated calendar_notifications table with a date column)
    lands."""
    assert not hasattr(db, "prune_runtime_state_calendar_notified")
