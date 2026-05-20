"""Phase 10: reminder MCP tool surface."""
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

@pytest.mark.asyncio
async def test_reminder_create_stores_row():
    from tools import reminders
    fire = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    out = await reminders.reminder_create.handler({"when_iso": fire, "text": "ping"})
    assert "data" in out
    rid = out["data"]["id"]
    row = db.reminder_get(rid)
    assert row["text"] == "ping"
    assert row["status"] == "active"

@pytest.mark.asyncio
async def test_reminder_create_rejects_past_time():
    from tools import reminders
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    out = await reminders.reminder_create.handler({"when_iso": past, "text": "x"})
    assert "refused" in out["content"][0]["text"].lower()

@pytest.mark.asyncio
async def test_reminder_create_with_lead_minutes():
    from tools import reminders
    fire = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    out = await reminders.reminder_create.handler({
        "when_iso": fire, "text": "meeting", "lead_minutes": 30,
    })
    row = db.reminder_get(out["data"]["id"])
    assert row["lead_minutes"] == 30

@pytest.mark.asyncio
async def test_reminder_create_with_repeat():
    from tools import reminders
    fire = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    out = await reminders.reminder_create.handler({
        "when_iso": fire, "text": "vitamins", "repeat": "daily",
    })
    row = db.reminder_get(out["data"]["id"])
    assert row["repeat"] == "daily"

@pytest.mark.asyncio
async def test_reminder_list_returns_active():
    from tools import reminders
    fire = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    await reminders.reminder_create.handler({"when_iso": fire, "text": "A"})
    await reminders.reminder_create.handler({"when_iso": fire, "text": "B"})
    out = await reminders.reminder_list.handler({"active_only": True})
    assert len(out["data"]["reminders"]) == 2

@pytest.mark.asyncio
async def test_reminder_cancel_marks_cancelled():
    from tools import reminders
    fire = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    out = await reminders.reminder_create.handler({"when_iso": fire, "text": "X"})
    rid = out["data"]["id"]
    await reminders.reminder_cancel.handler({"reminder_id": rid})
    assert db.reminder_get(rid)["status"] == "cancelled"

@pytest.mark.asyncio
async def test_reminder_snooze_advances_fire_at():
    from tools import reminders
    fire = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    out = await reminders.reminder_create.handler({"when_iso": fire, "text": "X"})
    rid = out["data"]["id"]
    await reminders.reminder_snooze.handler({"reminder_id": rid, "by_minutes": 30})
    row = db.reminder_get(rid)
    orig = datetime.fromisoformat(fire)
    new = datetime.fromisoformat(row["fire_at"])
    assert (new - orig).total_seconds() == 30 * 60


@pytest.mark.asyncio
async def test_reminder_snooze_requeues_gcal_sync_when_event_exists():
    """I-2: snooze must re-queue gcal_sync_pending so the external calendar
    event is updated to the new fire time."""
    from tools import reminders
    fire = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    out = await reminders.reminder_create.handler({"when_iso": fire, "text": "meeting"})
    rid = out["data"]["id"]
    # Simulate a successful prior sync: event_id stored, sync flag cleared.
    db.reminder_update_gcal_event(rid, "gcal_evt_abc123")
    row = db.reminder_get(rid)
    assert row["gcal_sync_pending"] == 0, "precondition: gcal_sync_pending cleared after sync"
    # Snooze should re-queue
    await reminders.reminder_snooze.handler({"reminder_id": rid, "by_minutes": 15})
    row = db.reminder_get(rid)
    assert row["gcal_sync_pending"] == 1, "snooze must flip gcal_sync_pending back to 1"


@pytest.mark.asyncio
async def test_reminder_snooze_requeues_apple_sync_when_event_exists():
    """I-2: snooze must re-queue apple_sync_pending so the Apple Reminder
    is updated to the new fire time."""
    from tools import reminders
    fire = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    out = await reminders.reminder_create.handler({"when_iso": fire, "text": "meeting"})
    rid = out["data"]["id"]
    # Simulate a successful prior Apple sync.
    db.reminder_update_apple_event(rid, "apple_evt_xyz789")
    row = db.reminder_get(rid)
    assert row["apple_sync_pending"] == 0, "precondition: apple_sync_pending cleared after sync"
    # Snooze should re-queue
    await reminders.reminder_snooze.handler({"reminder_id": rid, "by_minutes": 15})
    row = db.reminder_get(rid)
    assert row["apple_sync_pending"] == 1, "snooze must flip apple_sync_pending back to 1"


@pytest.mark.asyncio
async def test_reminder_snooze_does_not_requeue_when_never_synced():
    """I-2: if a reminder was never synced (event_id is NULL), snooze must not
    flip the sync flag from 0 to 1 — that would queue a spurious sync for a
    reminder the user opted out of syncing."""
    from tools import reminders
    fire = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    out = await reminders.reminder_create.handler({
        "when_iso": fire, "text": "no-sync reminder",
        "sync_to_gcal": False, "sync_to_apple": False,
    })
    rid = out["data"]["id"]
    row = db.reminder_get(rid)
    assert row["gcal_sync_pending"] == 0
    assert row["gcal_event_id"] is None
    await reminders.reminder_snooze.handler({"reminder_id": rid, "by_minutes": 10})
    row = db.reminder_get(rid)
    assert row["gcal_sync_pending"] == 0, "must not queue sync when event was never created"
    assert row["apple_sync_pending"] == 0, "must not queue apple sync when event was never created"
