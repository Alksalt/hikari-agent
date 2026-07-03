"""FIX 1: snoozing a reminder from its inline button must re-activate it.

fire_due_reminders flips a one-shot reminder to status='fired' before the
snooze keyboard is shown, and reminder_due() only re-selects 'active' rows —
so the old snooze branch (fire_at bump + requeue, no status change) left the
reminder permanently 'fired' and it never fired again.
"""
from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(_db_mod, "_DB_PATH", db_path)
    _db_mod._reset_schema_sentinel()
    yield


class _FakeBot:
    def __init__(self):
        self.messages: list[str] = []

    async def send_message(self, chat_id: int, text: str):
        self.messages.append(text)
        return None


@pytest.mark.asyncio
async def test_snooze_reactivates_fired_reminder():
    from agents.telegram_bridge import _cb_reminder
    from storage import db

    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    rid = db.reminder_insert(fire_at=past, text="drink water", lead_minutes=0, repeat=None)
    # Simulate fire_due_reminders having already fired it.
    db.reminder_mark_fired(rid)
    assert db.reminder_get(rid)["status"] == "fired"

    await _cb_reminder(_FakeBot(), 12345, "snooze", rid, "10m")

    row = db.reminder_get(rid)
    assert row["status"] == "active", "snooze must re-activate a fired reminder"
    # fire_at pushed into the future (~10m).
    assert row["fire_at"] > datetime.now(UTC).isoformat()


@pytest.mark.asyncio
async def test_snooze_refuses_cancelled_reminder():
    from agents.telegram_bridge import _cb_reminder
    from storage import db

    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    rid = db.reminder_insert(fire_at=past, text="old task", lead_minutes=0, repeat=None)
    db.reminder_cancel(rid)
    assert db.reminder_get(rid)["status"] == "cancelled"

    bot = _FakeBot()
    await _cb_reminder(bot, 12345, "snooze", rid, "10m")

    # Status unchanged; user told it can't be snoozed.
    assert db.reminder_get(rid)["status"] == "cancelled"
