"""Regression: WAL-inline sends must not be double-delivered by a concurrent drain.

Root cause (fixed 2026-05-31): send_and_persist inserted its write-ahead row in
status='pending', sent the message IN-LINE, then marked it 'sent'. Between the
insert and the mark_sent the 2-minute media_outbox drain
(_media_outbox_drain_job) could claim the still-'pending' row and send the SAME
text a second time. Reminders fire at top-of-hour (HH:00:43) and the 2-min drain
ticks on that same even-minute boundary, so EVERY top-of-hour reminder was
delivered twice (proven in the live log + media_outbox rows 193/194).

Fix: WAL-inline inserts pass claim_inline=True, landing the row directly in
status='sending'. The drain only claims status='pending' (media_outbox_claim),
so it structurally cannot grab a live in-line send. Crash recovery is unchanged:
the stale-sending reaper re-queues a row whose in-line sender died before
mark_sent.

These tests fail on the pre-fix code and pass after.
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


def _one(rid: int) -> dict:
    with db._conn() as c:
        return dict(c.execute("SELECT * FROM media_outbox WHERE id=?", (rid,)).fetchone())


def test_claim_inline_insert_lands_sending_not_pending():
    """A claim_inline insert is pre-claimed: status='sending', processed_at stamped."""
    rid = db.media_outbox_insert(
        "text", "wal-1", {"chat_id": 7, "text": "hi"}, claim_inline=True
    )
    assert rid is not None
    row = _one(rid)
    assert row["status"] == "sending"
    assert row["processed_at"] is not None


def test_drain_cannot_claim_a_live_wal_inline_row():
    """The exact double-send race: a drain firing between insert and mark_sent.

    Simulate send_and_persist's WAL insert (claim_inline=True). BEFORE the
    in-line mark_sent, fire the drain's claim primitive. It must return NOTHING
    because the row is already 'sending' (pre-claimed), not 'pending'. Then the
    in-line mark_sent finalizes it. Net: exactly one owner, one delivery.
    """
    rid = db.media_outbox_insert(
        "text", "wal-race", {"chat_id": 7, "text": "reminder body"}, claim_inline=True
    )
    assert rid is not None

    # Concurrent drain fires mid-send. Pre-fix: row is 'pending' -> claimed -> 2nd send.
    # Post-fix: row is 'sending' -> claim returns [] -> no second send.
    claimed = db.media_outbox_claim("text")
    assert claimed == [], "drain double-claimed a live in-line send (double-send bug)"

    # In-line send completes and marks sent.
    db.media_outbox_mark_sent(rid, telegram_message_id=740)
    row = _one(rid)
    assert row["status"] == "sent"
    assert row["telegram_message_id"] == 740


def test_enqueue_only_insert_still_pending_and_drainable():
    """Default (claim_inline=False) inserts stay 'pending' so the drain delivers them.

    Guards the enqueue-only kinds (voice / generated photo / reconciler) against
    regression: the fix must NOT make every insert pre-claimed.
    """
    rid = db.media_outbox_insert("voice", "enq-1", {"chat_id": 7})
    assert rid is not None
    claimed = db.media_outbox_claim("voice")
    assert len(claimed) == 1
    assert claimed[0]["id"] == rid
    assert claimed[0]["status"] == "sending"


def test_stale_wal_inline_row_is_recovered_by_reaper():
    """Crash recovery: an in-line sender that dies before mark_sent leaves a
    'sending' row whose processed_at ages out; the reaper re-queues it to
    'pending' for the drain. Recovery parity with a normally-claimed row.
    """
    rid = db.media_outbox_insert(
        "text", "wal-crash", {"chat_id": 7, "text": "x"}, claim_inline=True
    )
    # Age processed_at past the 300s grace (simulate the sender crashing long ago).
    with db._conn() as c:
        c.execute(
            "UPDATE media_outbox SET processed_at = datetime('now', '-400 seconds') "
            "WHERE id=?",
            (rid,),
        )
    count = db.media_outbox_reap_stale_sending(grace_seconds=300)
    assert count == 1
    assert _one(rid)["status"] == "pending"
    # And now the drain CAN claim the recovered row.
    claimed = db.media_outbox_claim("text")
    assert len(claimed) == 1
    assert claimed[0]["id"] == rid
