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
