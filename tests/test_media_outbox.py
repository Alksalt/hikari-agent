"""Tests for media_outbox DB helpers (Sprint 7A).

Covers:
  - insert dedup via idempotency_key (INSERT OR IGNORE)
  - helpers parity with graph_outbox pattern
  - mark_sent / mark_failed / mark_aborted status transitions
  - stats counts correctly
  - conn= injection for transactional use
"""
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
    db._reset_schema_sentinel()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _all_rows():
    with db._conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM media_outbox ORDER BY id").fetchall()]


def _insert(kind="text", key="key1", payload=None):
    return db.media_outbox_insert(kind, key, payload or {"body": "hello"})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_insert_returns_row_id():
    row_id = _insert()
    assert isinstance(row_id, int)
    assert row_id > 0


def test_insert_dedup_returns_none():
    _insert(key="dup")
    result = _insert(key="dup")
    assert result is None


def test_insert_creates_pending_row():
    _insert(key="k1")
    rows = _all_rows()
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert rows[0]["attempts"] == 0


def test_pending_returns_oldest_first():
    import time
    _insert(key="old", kind="text")
    time.sleep(0.01)
    _insert(key="new", kind="text")
    rows = db.media_outbox_pending()
    assert rows[0]["idempotency_key"] == "old"
    assert rows[1]["idempotency_key"] == "new"


def test_pending_kind_filter():
    _insert(key="txt", kind="text")
    _insert(key="img", kind="photo")
    text_rows = db.media_outbox_pending(kind="text")
    photo_rows = db.media_outbox_pending(kind="photo")
    assert len(text_rows) == 1
    assert len(photo_rows) == 1
    assert text_rows[0]["kind"] == "text"
    assert photo_rows[0]["kind"] == "photo"


def test_mark_sent_transitions_status():
    row_id = _insert(key="s1")
    db.media_outbox_mark_sent(row_id, telegram_message_id=999)
    rows = _all_rows()
    assert rows[0]["status"] == "sent"
    assert rows[0]["telegram_message_id"] == 999
    assert rows[0]["processed_at"] is not None


def test_mark_failed_increments_attempts():
    row_id = _insert(key="f1")
    db.media_outbox_mark_failed(row_id, "timeout")
    rows = _all_rows()
    assert rows[0]["attempts"] == 1
    assert rows[0]["status"] == "pending"  # not yet at 5
    assert rows[0]["last_error"] == "timeout"


def test_mark_failed_flips_to_failed_at_5_attempts():
    row_id = _insert(key="f5")
    for i in range(5):
        db.media_outbox_mark_failed(row_id, f"err{i}")
    rows = _all_rows()
    assert rows[0]["status"] == "failed"
    assert rows[0]["attempts"] == 5


def test_mark_aborted_transitions_status():
    row_id = _insert(key="a1")
    db.media_outbox_mark_aborted(row_id, "file_missing")
    rows = _all_rows()
    assert rows[0]["status"] == "aborted"
    assert rows[0]["last_error"] == "file_missing"


def test_stats_counts_correctly():
    r1 = _insert(key="s1")
    r2 = _insert(key="s2")
    _insert(key="f1")
    db.media_outbox_mark_sent(r1, None)
    db.media_outbox_mark_aborted(r2, "reason")
    # third row stays pending
    stats = db.media_outbox_stats()
    assert stats["sent"] == 1
    assert stats["aborted"] == 1
    assert stats["pending"] == 1
    assert stats["failed"] == 0


def test_insert_with_conn_shares_transaction():
    """conn= kwarg: insert inside caller's transaction, no auto-commit."""
    with db._conn() as c:
        row_id = db.media_outbox_insert("sticker", "conn_key", {"x": 1}, conn=c)
    # After _conn().__exit__, commit happened.
    rows = _all_rows()
    assert len(rows) == 1
    assert row_id is not None


def test_pending_respects_limit():
    for i in range(10):
        _insert(key=f"k{i}")
    rows = db.media_outbox_pending(limit=3)
    assert len(rows) == 3


def test_sent_rows_not_returned_by_pending():
    r1 = _insert(key="done")
    db.media_outbox_mark_sent(r1, None)
    _insert(key="still_pending")
    rows = db.media_outbox_pending()
    assert len(rows) == 1
    assert rows[0]["idempotency_key"] == "still_pending"
