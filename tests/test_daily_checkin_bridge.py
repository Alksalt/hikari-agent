"""Bridge short-circuits: schedule edits and check-in replies are pre-routed."""
from __future__ import annotations

import importlib
from datetime import UTC, datetime
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    monkeypatch.setenv("HOME_TZ", "Europe/Berlin")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from agents import config
    config.reload()
    yield


@pytest.mark.asyncio
async def test_handle_schedule_edit_override(monkeypatch):
    """The bridge helper handle_message detects schedule edits
    and applies them, returning a short ack to send."""
    from agents import daily_checkin
    consumed, ack = await daily_checkin.handle_message(
        "check in at 06:30 tomorrow",
        today=datetime(2026, 5, 21).date(),
        send_text=None,
    )
    assert consumed is True
    assert ack is not None
    s = daily_checkin._load_schedule()
    assert s["override_date"] == "2026-05-22"
    assert s["override_time"] == "06:30"


@pytest.mark.asyncio
async def test_handle_schedule_query(monkeypatch):
    from agents import daily_checkin
    consumed, ack = await daily_checkin.handle_message(
        "what time is my check-in?",
        today=datetime(2026, 5, 21).date(),
        send_text=None,
    )
    assert consumed is True
    assert ack is not None
    assert "07:00" in ack or "default" in ack.lower()


@pytest.mark.asyncio
async def test_handle_pending_reply_consumes(monkeypatch):
    from agents import daily_checkin
    from storage import db
    db.runtime_set(daily_checkin.PENDING_KEY,
                   datetime.now(UTC).isoformat())
    sent: list[str] = []
    async def fake_send(s):
        sent.append(s)
        return (s, 1, True)
    from unittest.mock import AsyncMock
    monkeypatch.setattr(daily_checkin, "fetch_email_buckets", AsyncMock(return_value={
        "unread_personal": [], "calendar_invites": [],
        "deletable": {"count": 0, "top_senders": [], "sample_ids": []},
    }))
    monkeypatch.setattr(daily_checkin, "fetch_calendar_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(daily_checkin, "compose_email_message",
                        AsyncMock(return_value="nothing in inbox."))
    monkeypatch.setattr(daily_checkin, "compose_calendar_message",
                        AsyncMock(return_value="calendar empty."))
    consumed, ack = await daily_checkin.handle_message(
        "yes", today=datetime(2026, 5, 21).date(), send_text=fake_send,
    )
    assert consumed is True
    assert ack is None  # send_text already used for the per-topic outputs
    assert sent == ["nothing in inbox.", "calendar empty."]


@pytest.mark.asyncio
async def test_handle_normal_message_not_consumed(monkeypatch):
    from agents import daily_checkin
    consumed, ack = await daily_checkin.handle_message(
        "hey, what's the weather",
        today=datetime(2026, 5, 21).date(),
        send_text=None,
    )
    assert consumed is False
    assert ack is None
