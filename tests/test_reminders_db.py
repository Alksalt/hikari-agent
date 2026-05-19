"""Phase 10: reminders table CRUD."""
from __future__ import annotations
import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
import pytest

from storage import db
from agents import config


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
