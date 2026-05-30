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
    # ORDER BY created_at ASC, id ASC — id is the tiebreaker so no sleep needed.
    _insert(key="old", kind="text")
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
    # For photo rows the 5-attempt budget applies — still pending after one failure.
    row_id = _insert(kind="photo", key="f1")
    db.media_outbox_mark_failed(row_id, "timeout")
    rows = _all_rows()
    assert rows[0]["attempts"] == 1
    assert rows[0]["status"] == "pending"  # photo: not yet at 5
    assert rows[0]["last_error"] == "timeout"


def test_mark_failed_flips_to_failed_at_5_attempts():
    # Photo rows have a 5-attempt retry budget.
    row_id = _insert(kind="photo", key="f5")
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


def test_mark_failed_terminal_for_non_retried_kinds():
    """text/sticker rows flip to 'failed' on first failure; photo keeps budget of 5."""
    # Text: terminal on first failure.
    text_id = _insert(kind="text", key="txt_fail")
    db.media_outbox_mark_failed(text_id, "boom")
    rows = _all_rows()
    txt_row = next(r for r in rows if r["idempotency_key"] == "txt_fail")
    assert txt_row["status"] == "failed"
    assert txt_row["attempts"] == 1

    # Photo: still pending after first failure.
    photo_id = _insert(kind="photo", key="photo_fail")
    db.media_outbox_mark_failed(photo_id, "net err")
    rows = _all_rows()
    photo_row = next(r for r in rows if r["idempotency_key"] == "photo_fail")
    assert photo_row["status"] == "pending"
    assert photo_row["attempts"] == 1

    # Photo: flips to failed after 5 total failures.
    for i in range(4):
        db.media_outbox_mark_failed(photo_id, f"retry {i}")
    rows = _all_rows()
    photo_row = next(r for r in rows if r["idempotency_key"] == "photo_fail")
    assert photo_row["status"] == "failed"
    assert photo_row["attempts"] == 5


# ---------------------------------------------------------------------------
# claim + reap tests (Phase 3D / D14)
# ---------------------------------------------------------------------------

def test_claim_flips_pending_to_sending_and_returns():
    """claim returns the rows AND flips them to 'sending'; a second claim gets 0."""
    _insert(kind="text", key="c1")
    _insert(kind="text", key="c2")

    claimed = db.media_outbox_claim("text")
    assert len(claimed) == 2
    assert all(r["status"] == "sending" for r in claimed)

    # DB state matches
    rows = _all_rows()
    assert all(r["status"] == "sending" for r in rows)

    # No double-claim
    claimed2 = db.media_outbox_claim("text")
    assert claimed2 == []


def test_claim_respects_kind_filter():
    """Claiming 'text' leaves 'photo' rows untouched."""
    _insert(kind="text", key="txt1")
    _insert(kind="photo", key="img1")

    claimed = db.media_outbox_claim("text")
    assert len(claimed) == 1
    assert claimed[0]["kind"] == "text"

    rows = _all_rows()
    photo_row = next(r for r in rows if r["kind"] == "photo")
    assert photo_row["status"] == "pending"


def test_claim_respects_limit():
    """With 3 pending and limit=2, only 2 are claimed; 1 stays pending."""
    _insert(kind="text", key="l1")
    _insert(kind="text", key="l2")
    _insert(kind="text", key="l3")

    claimed = db.media_outbox_claim("text", limit=2)
    assert len(claimed) == 2

    pending_after = db.media_outbox_pending(kind="text")
    assert len(pending_after) == 1


def test_reap_stale_sending_resets_old_rows():
    """A row in 'sending' whose processed_at (claim time) predates the cutoff is reset to 'pending'."""
    row_id = _insert(kind="text", key="stale1")
    db.media_outbox_claim("text")  # now 'sending', processed_at = now

    # Back-date processed_at so it looks like the claim happened 1 hour ago
    with db._conn() as c:
        c.execute(
            "UPDATE media_outbox SET processed_at = datetime('now', '-3600 seconds') WHERE id=?",
            (row_id,),
        )

    count = db.media_outbox_reap_stale_sending(grace_seconds=300)
    assert count == 1

    rows = _all_rows()
    assert rows[0]["status"] == "pending"


def test_reap_leaves_fresh_sending_alone():
    """A freshly-claimed row (just now) is NOT reaped under the default 300 s grace."""
    _insert(kind="text", key="fresh1")
    db.media_outbox_claim("text")  # 'sending', processed_at = now

    count = db.media_outbox_reap_stale_sending(grace_seconds=300)
    assert count == 0

    rows = _all_rows()
    assert rows[0]["status"] == "sending"


def test_reap_leaves_aged_pending_just_claimed_alone():
    """Double-send regression: a row created long ago but freshly claimed must NOT be reaped.

    Scenario: row sits 'pending' for >300s (e.g. 1 hour) before the drain fires and
    claims it. The reaper must NOT reset it to 'pending' mid-send just because
    created_at is old — it should only look at processed_at (claim time).
    """
    row_id = _insert(kind="text", key="aged_pending")

    # Simulate the row having been inserted 1 hour ago (long pending wait).
    with db._conn() as c:
        c.execute(
            "UPDATE media_outbox SET created_at = datetime('now', '-3600 seconds') WHERE id=?",
            (row_id,),
        )

    # Now claim it — processed_at is stamped as NOW.
    claimed = db.media_outbox_claim("text")
    assert len(claimed) == 1
    assert claimed[0]["status"] == "sending"

    # Reaper runs with 300s grace — processed_at is fresh (just now), so row must survive.
    count = db.media_outbox_reap_stale_sending(grace_seconds=300)
    assert count == 0

    rows = _all_rows()
    assert rows[0]["status"] == "sending"


def test_reconciler_refuses_symlinks(tmp_path, monkeypatch):
    """_reconcile_photo_outbox_orphans unlinks symlinks and inserts no DB row."""
    import importlib

    # Set up an isolated DB.
    db_path = tmp_path / "hikari_sym.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()

    # Create a fake outbox dir with a real PNG and a symlink.
    outbox_dir = tmp_path / "photo_outbox"
    outbox_dir.mkdir()
    real_png = tmp_path / "real.png"
    real_png.write_bytes(b"\x89PNG\r\n")
    target_file = tmp_path / "secret.txt"
    target_file.write_text("secret")
    symlink = outbox_dir / "evil.png"
    symlink.symlink_to(target_file)

    # Patch PHOTO_OUTBOX to point to our temp dir.
    import agents.telegram_bridge as bridge_mod
    monkeypatch.setattr(bridge_mod, "PHOTO_OUTBOX", outbox_dir)
    # Also patch db reference inside the bridge to the reloaded module.
    monkeypatch.setattr(bridge_mod, "db", _db_mod)

    bridge_mod._reconcile_photo_outbox_orphans()

    # Symlink must be gone.
    assert not symlink.exists()
    assert not symlink.is_symlink()

    # No media_outbox row should have been inserted.
    with _db_mod._conn() as c:
        count = c.execute("SELECT COUNT(*) FROM media_outbox").fetchone()[0]
    assert count == 0
