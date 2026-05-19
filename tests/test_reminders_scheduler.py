"""Phase 10: scheduler fires due reminders + handles repeats."""
from __future__ import annotations
import asyncio
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
async def test_fire_due_reminders_sends_text_and_marks_fired():
    sent: list[str] = []
    async def fake_send(s: str): sent.append(s)
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    rid = db.reminder_insert(fire_at=past, text="ping", lead_minutes=0, repeat=None)
    from agents import proactive
    await proactive.fire_due_reminders(fake_send)
    assert any("ping" in s for s in sent)
    assert db.reminder_get(rid)["status"] == "fired"

@pytest.mark.asyncio
async def test_repeat_daily_reinserts_next_day():
    sent: list[str] = []
    async def fake_send(s: str): sent.append(s)
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    rid = db.reminder_insert(fire_at=past, text="vitamins",
                             lead_minutes=0, repeat="daily")
    from agents import proactive
    await proactive.fire_due_reminders(fake_send)
    assert db.reminder_get(rid)["status"] == "fired"
    all_rows = db.reminder_list(active_only=False)
    active = [r for r in all_rows if r["status"] == "active"]
    assert len(active) == 1
    next_fire = datetime.fromisoformat(active[0]["fire_at"])
    orig = datetime.fromisoformat(past)
    assert (next_fire - orig).days == 1

@pytest.mark.asyncio
async def test_gcal_sync_pending_clears_after_mock_subagent(monkeypatch):
    monkeypatch.setenv("GOOGLE_WORKSPACE_REFRESH_TOKEN", "fake-refresh-token")
    fire = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    rid = db.reminder_insert(
        fire_at=fire, text="meeting", lead_minutes=0, repeat=None,
        gcal_sync_pending=True,
    )
    from agents import proactive
    async def fake_run_proactive(prompt, **kwargs):
        return "event_id: 'abc123xyz'\n"
    monkeypatch.setattr(proactive, "run_proactive", fake_run_proactive)
    n = await proactive.sync_pending_gcal_reminders()
    assert n == 1
    row = db.reminder_get(rid)
    assert row["gcal_sync_pending"] == 0
    assert row["gcal_event_id"] == "abc123xyz"
