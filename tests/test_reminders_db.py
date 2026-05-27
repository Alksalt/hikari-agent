"""Phase 10: reminders table CRUD."""
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


def test_reminder_insert_and_list():
    fire_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    rid = db.reminder_insert(
        fire_at=fire_at, text="ping the dentist",
        lead_minutes=0, repeat=None, gcal_event_id=None,
    )
    assert rid > 0
    rows = db.reminder_list(active_only=True)
    assert len(rows) == 1
    assert rows[0]["text"] == "ping the dentist"
    assert rows[0]["status"] == "active"


def test_reminder_due_returns_only_past_active():
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    db.reminder_insert(fire_at=past, text="overdue", lead_minutes=0, repeat=None)
    db.reminder_insert(fire_at=future, text="upcoming", lead_minutes=0, repeat=None)
    due = db.reminder_due()
    assert len(due) == 1
    assert due[0]["text"] == "overdue"


def test_reminder_mark_fired_advances_status():
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    rid = db.reminder_insert(fire_at=past, text="x", lead_minutes=0, repeat=None)
    db.reminder_mark_fired(rid)
    assert db.reminder_list(active_only=True) == []
    assert len(db.reminder_list(active_only=False)) == 1


def test_reminder_cancel_sets_status():
    fire_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    rid = db.reminder_insert(fire_at=fire_at, text="x", lead_minutes=0, repeat=None)
    db.reminder_cancel(rid)
    assert db.reminder_list(active_only=True) == []


# ---------------------------------------------------------------------------
# Phase 15: action-mode reminders — schema + helpers
# ---------------------------------------------------------------------------

def test_action_mode_columns_present_after_migration():
    """Migration runs at first _conn() — the new columns must be queryable."""
    # Touch the DB once to trigger schema bootstrap + migrations.
    db.reminder_insert(
        fire_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        text="touch",
    )
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(str(db._DB_PATH))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(reminders)").fetchall()}
    conn.close()
    expected = {
        "kind", "seed_prompt", "max_fires", "fires_done",
        "consecutive_failures", "summary_prompt",
        "budget_usd_per_fire", "timeout_s",
    }
    missing = expected - cols
    assert not missing, f"missing action-mode columns: {missing}"


def test_action_mode_index_present():
    db.reminder_insert(
        fire_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        text="touch",
    )
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(str(db._DB_PATH))
    idx_names = {r[1] for r in conn.execute(
        "SELECT * FROM sqlite_master WHERE type = 'index' AND tbl_name = 'reminders'"
    ).fetchall()}
    conn.close()
    assert "idx_reminders_action_active" in idx_names


def test_text_reminder_defaults():
    """Default kind is 'text'; fires_done starts at 0; max_fires NULL."""
    rid = db.reminder_insert(
        fire_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        text="hi",
    )
    row = db.reminder_get(rid)
    assert row is not None
    assert row["kind"] == "text"
    assert row["fires_done"] == 0
    assert row["consecutive_failures"] == 0
    assert row["max_fires"] is None
    assert row["seed_prompt"] is None


def test_action_reminder_round_trip():
    rid = db.reminder_insert(
        fire_at=(datetime.now(UTC) + timedelta(minutes=20)).isoformat(),
        text="autonomous notion write",
        recurrence_rule="every_n_minutes:20",
        kind="action",
        seed_prompt="write next row to notion db abc123",
        max_fires=6,
        summary_prompt="summarize what was written",
        budget_usd_per_fire=0.40,
        timeout_s=180,
    )
    row = db.reminder_get(rid)
    assert row["kind"] == "action"
    assert row["seed_prompt"] == "write next row to notion db abc123"
    assert row["max_fires"] == 6
    assert row["summary_prompt"] == "summarize what was written"
    assert row["budget_usd_per_fire"] == 0.40
    assert row["timeout_s"] == 180


def test_invalid_kind_rejected():
    with pytest.raises(ValueError, match="kind"):
        db.reminder_insert(
            fire_at=(datetime.now(UTC) + timedelta(minutes=20)).isoformat(),
            text="x",
            kind="weird",
        )


def test_increment_fires_done_atomic():
    rid = db.reminder_insert(
        fire_at=(datetime.now(UTC) + timedelta(minutes=20)).isoformat(),
        text="x", kind="action", seed_prompt="x", max_fires=6,
    )
    assert db.reminder_increment_fires_done(rid) == 1
    assert db.reminder_increment_fires_done(rid) == 2
    assert db.reminder_increment_fires_done(rid) == 3
    row = db.reminder_get(rid)
    assert row["fires_done"] == 3


def test_increment_and_reset_failures():
    rid = db.reminder_insert(
        fire_at=(datetime.now(UTC) + timedelta(minutes=20)).isoformat(),
        text="x", kind="action", seed_prompt="x", max_fires=6,
    )
    assert db.reminder_increment_failures(rid) == 1
    assert db.reminder_increment_failures(rid) == 2
    db.reminder_reset_failures(rid)
    row = db.reminder_get(rid)
    assert row["consecutive_failures"] == 0


def test_set_status():
    rid = db.reminder_insert(
        fire_at=(datetime.now(UTC) + timedelta(minutes=20)).isoformat(),
        text="x",
    )
    db.reminder_set_status(rid, "cancelled")
    row = db.reminder_get(rid)
    assert row["status"] == "cancelled"


def test_migration_idempotent():
    """Running the migration twice on the same DB must not error."""
    # First call creates the schema (already happened via the fixture).
    db.reminder_insert(
        fire_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        text="warm",
    )
    # Manually re-invoke the migration — must be a no-op.
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(str(db._DB_PATH))
    conn.row_factory = _sqlite3.Row
    try:
        db._migrate_reminders_action_mode(conn)
        db._migrate_reminders_action_mode(conn)
    finally:
        conn.close()
    # And the table should still be queryable + writable.
    db.reminder_insert(
        fire_at=(datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        text="post-rerun",
    )
    assert len(db.reminder_list(active_only=True)) >= 2
