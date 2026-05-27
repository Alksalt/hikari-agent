"""Phase 10: scheduler fires due reminders + handles repeats."""
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

@pytest.fixture(autouse=True)
def _gate_open(monkeypatch):
    """Keep the proactive gate open in unit tests — quiet-hours / silence
    checks are covered by tests/test_proactive_global_reservation.py."""
    import agents.proactive_gate as _gate
    monkeypatch.setattr(_gate, "_is_quiet_now", lambda _db=None: False)
    monkeypatch.setattr(_gate, "_silence_active", lambda _db: False)

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
async def test_reminder_push_has_emoji_marker():
    """Fired reminder push text must start with the ⏰ emoji, not 'reminder:'."""
    sent: list[str] = []
    async def fake_send(s: str): sent.append(s)
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    db.reminder_insert(fire_at=past, text="take vitamins", lead_minutes=0, repeat=None)
    from agents import proactive
    await proactive.fire_due_reminders(fake_send)
    assert sent, "expected at least one message to be sent"
    assert sent[0].startswith("⏰"), f"expected ⏰ prefix, got: {sent[0]!r}"
    assert "take vitamins" in sent[0]


@pytest.mark.asyncio
async def test_reminder_push_no_double_prefix():
    """If the stored text already starts with ⏰, it must not be double-prefixed."""
    sent: list[str] = []
    async def fake_send(s: str): sent.append(s)
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    db.reminder_insert(fire_at=past, text="⏰ already marked", lead_minutes=0, repeat=None)
    from agents import proactive
    await proactive.fire_due_reminders(fake_send)
    assert sent, "expected at least one message to be sent"
    assert sent[0] == "⏰ already marked", f"unexpected double-prefix: {sent[0]!r}"


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
    # After the clamp fix, base = max(past, now) ≈ now, so next_fire ≈ now+1day.
    # Verify it's in the future and within a 25-hour window from now.
    now = datetime.now(UTC)
    assert next_fire > now, f"next fire_at {next_fire} should be in the future"
    assert (next_fire - now).total_seconds() < 25 * 3600, \
        f"next fire_at {next_fire} should be ~1 day from now, not further"

@pytest.mark.asyncio
async def test_overdue_daily_repeat_advances_to_future_not_past():
    """If the scheduler was delayed 3 days, a daily reminder should fire once
    and the next occurrence should be in the FUTURE, not 2 days in the past."""
    sent: list[str] = []
    async def fake_send(s: str): sent.append(s)
    three_days_ago = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    rid = db.reminder_insert(
        fire_at=three_days_ago, text="vitamins",
        lead_minutes=0, repeat="daily",
    )
    from agents import proactive
    await proactive.fire_due_reminders(fake_send)
    # original fired once
    assert db.reminder_get(rid)["status"] == "fired"
    # next-occurrence row must be in the FUTURE
    active = [r for r in db.reminder_list(active_only=False)
              if r["status"] == "active"]
    assert len(active) == 1
    next_fire = datetime.fromisoformat(active[0]["fire_at"])
    assert next_fire > datetime.now(UTC), \
        f"next fire_at {next_fire} should be in the future"


@pytest.mark.asyncio
async def test_gcal_sync_pending_clears_after_mock_subagent(monkeypatch):
    from unittest.mock import patch

    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_SECRET", "fake-client-secret")
    monkeypatch.setenv("GOOGLE_WORKSPACE_REFRESH_TOKEN", "fake-refresh-token")
    fire = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    rid = db.reminder_insert(
        fire_at=fire, text="meeting", lead_minutes=0, repeat=None,
        gcal_sync_pending=True,
    )
    from agents import proactive
    from tools.reminders.sync_gcal import GCalReminderResult

    async def fake_sync_gcal(reminder_id, title, start_iso, calendar_id="primary"):
        db.reminder_update_gcal_event(reminder_id, "abc123xyz")
        return GCalReminderResult(reminder_id=reminder_id, gcal_event_id="abc123xyz")

    with patch("tools.reminders.sync_gcal._sync_gcal_reminder", side_effect=fake_sync_gcal):
        n = await proactive.sync_pending_gcal_reminders()
    assert n == 1
    row = db.reminder_get(rid)
    assert row["gcal_sync_pending"] == 0
    assert row["gcal_event_id"] == "abc123xyz"


@pytest.mark.asyncio
async def test_apple_sync_pending_clears_after_mock_subagent(monkeypatch):
    from unittest.mock import patch

    fire = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    rid = db.reminder_insert(
        fire_at=fire, text="grocery list", lead_minutes=0, repeat=None,
        gcal_sync_pending=False,
    )
    # Manually flip apple_sync_pending on (simulating reminder_create with sync_to_apple=True)
    from storage.db import _conn
    with _conn() as conn:
        conn.execute("UPDATE reminders SET apple_sync_pending=1 WHERE id=?", (rid,))
    from agents import proactive
    from tools.reminders.sync_apple import AppleReminderResult

    async def fake_sync_apple(reminder_id, title, due_iso, list_name="Reminders"):
        db.reminder_update_apple_event(reminder_id, "ABC-EVENT-123")
        return AppleReminderResult(reminder_id=reminder_id, apple_event_id="ABC-EVENT-123")

    # Patch sys.platform to darwin so the guard passes regardless of test host
    import sys
    monkeypatch.setattr(sys, "platform", "darwin")
    with patch("tools.reminders.sync_apple._sync_apple_reminder", side_effect=fake_sync_apple):
        n = await proactive.sync_pending_apple_reminders()
    assert n == 1
    row = db.reminder_get(rid)
    assert row["apple_sync_pending"] == 0
    assert row["apple_event_id"] == "ABC-EVENT-123"
