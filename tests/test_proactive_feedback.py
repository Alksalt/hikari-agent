"""Phase D: proactive_events feedback column tests.

Covers:
  - Migration idempotency (columns survive double-migration)
  - proactive_event_record_reaction: up/down increments
  - proactive_event_record_reaction: no-op on missing telegram_message_id
  - proactive_event_record_silence_window: flips silenced_within_1h for recent rows
  - proactive_event_record_silence_window: ignores rows older than 1h
"""
from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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
    yield


def _insert_event(tg_id: int | None = 1001, sent_offset_minutes: int = 0) -> int:
    """Insert a proactive_events row. Returns the row id."""
    # Temporarily override _now if we need a past row
    if sent_offset_minutes != 0:
        real_now = db._now
        past = (datetime.now(UTC) - timedelta(minutes=abs(sent_offset_minutes))).isoformat()
        db._now = lambda: past  # type: ignore[assignment]
    try:
        row_id = db.proactive_event_insert(
            source="test_source",
            pattern="test_pattern",
            payload_json="{}",
            telegram_message_id=tg_id,
        )
    finally:
        if sent_offset_minutes != 0:
            db._now = real_now  # type: ignore[assignment]
    return row_id


# ---------- migration idempotency ----------

def test_feedback_columns_exist_after_migration():
    """Columns thumbs_up/down/silenced/reaction_received_at must exist."""
    with db._conn() as c:
        cols = {r["name"] for r in c.execute(
            "PRAGMA table_info(proactive_events)"
        ).fetchall()}
    assert "thumbs_up" in cols, "thumbs_up column missing"
    assert "thumbs_down" in cols, "thumbs_down column missing"
    assert "silenced_within_1h" in cols, "silenced_within_1h column missing"
    assert "reaction_received_at" in cols, "reaction_received_at column missing"


def test_migration_is_idempotent(tmp_path, monkeypatch):
    """Running _migrate_proactive_events_feedback twice doesn't raise."""
    with db._conn() as c:
        db._migrate_proactive_events_feedback(c)  # second call
        db._migrate_proactive_events_feedback(c)  # third call (idempotent)
    # If we got here without an exception, idempotency holds.


# ---------- reaction recording ----------

def test_record_reaction_up_increments_thumbs_up():
    _insert_event(tg_id=111)
    rows = db.proactive_event_record_reaction(111, "up")
    assert rows == 1
    with db._conn() as c:
        row = c.execute(
            "SELECT thumbs_up, thumbs_down FROM proactive_events WHERE telegram_message_id = 111"
        ).fetchone()
    assert row["thumbs_up"] == 1
    assert row["thumbs_down"] == 0


def test_record_reaction_down_increments_thumbs_down():
    _insert_event(tg_id=222)
    db.proactive_event_record_reaction(222, "down")
    with db._conn() as c:
        row = c.execute(
            "SELECT thumbs_up, thumbs_down FROM proactive_events WHERE telegram_message_id = 222"
        ).fetchone()
    assert row["thumbs_up"] == 0
    assert row["thumbs_down"] == 1


def test_record_reaction_up_accumulates():
    """Multiple up reactions accumulate."""
    _insert_event(tg_id=333)
    db.proactive_event_record_reaction(333, "up")
    db.proactive_event_record_reaction(333, "up")
    with db._conn() as c:
        row = c.execute(
            "SELECT thumbs_up FROM proactive_events WHERE telegram_message_id = 333"
        ).fetchone()
    assert row["thumbs_up"] == 2


def test_record_reaction_stamps_received_at_once():
    """reaction_received_at is stamped on first reaction and not overwritten."""
    _insert_event(tg_id=444)
    db.proactive_event_record_reaction(444, "up")
    with db._conn() as c:
        row = c.execute(
            "SELECT reaction_received_at FROM proactive_events WHERE telegram_message_id = 444"
        ).fetchone()
    first_ts = row["reaction_received_at"]
    assert first_ts is not None

    db.proactive_event_record_reaction(444, "down")
    with db._conn() as c:
        row2 = c.execute(
            "SELECT reaction_received_at FROM proactive_events WHERE telegram_message_id = 444"
        ).fetchone()
    assert row2["reaction_received_at"] == first_ts, "reaction_received_at should not be overwritten"


def test_record_reaction_no_matching_row_returns_zero():
    """Returns 0 if no row has the given telegram_message_id."""
    result = db.proactive_event_record_reaction(99999, "up")
    assert result == 0


def test_record_reaction_null_tg_id_row_not_matched():
    """Row with telegram_message_id=NULL should not be matched by any reaction."""
    _insert_event(tg_id=None)
    # There's no valid tg_id to match, so a lookup on a non-existent id returns 0
    result = db.proactive_event_record_reaction(0, "up")
    assert result == 0


# ---------- silence window ----------

def test_silence_window_flips_recent_rows():
    """Rows sent within the last hour get silenced_within_1h=1."""
    _insert_event(tg_id=555, sent_offset_minutes=30)  # 30 min ago
    rows_updated = db.proactive_event_record_silence_window()
    assert rows_updated >= 1
    with db._conn() as c:
        row = c.execute(
            "SELECT silenced_within_1h FROM proactive_events WHERE telegram_message_id = 555"
        ).fetchone()
    assert row["silenced_within_1h"] == 1


def test_silence_window_ignores_old_rows():
    """Rows sent more than 1h ago are NOT flagged."""
    _insert_event(tg_id=666, sent_offset_minutes=90)  # 90 min ago — outside window
    db.proactive_event_record_silence_window()
    with db._conn() as c:
        row = c.execute(
            "SELECT silenced_within_1h FROM proactive_events WHERE telegram_message_id = 666"
        ).fetchone()
    assert row["silenced_within_1h"] == 0


def test_silence_window_returns_row_count():
    """Return value equals the number of rows updated."""
    _insert_event(tg_id=777, sent_offset_minutes=5)
    _insert_event(tg_id=778, sent_offset_minutes=10)
    result = db.proactive_event_record_silence_window()
    assert result == 2
