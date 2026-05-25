"""End-to-end orchestration: schedule fires → question sent → user replies →
topic fetches run → topic messages sent → pending state cleared."""
from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

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


@pytest.fixture(autouse=True)
def _gate_open(monkeypatch):
    import agents.proactive_gate as _gate
    monkeypatch.setattr(_gate, "_is_quiet_now", lambda _db=None: False)
    monkeypatch.setattr(_gate, "_silence_active", lambda _db: False)


def _at_target(hh: int = 7, mm: int = 0) -> datetime:
    import zoneinfo
    tz = zoneinfo.ZoneInfo("Europe/Berlin")
    return datetime(2026, 5, 21, hh, mm, tzinfo=tz)


@pytest.mark.asyncio
async def test_does_not_fire_outside_window(monkeypatch):
    from agents import daily_checkin
    monkeypatch.setattr(daily_checkin, "_now_local", lambda: _at_target(hh=10))
    sent: list[str] = []
    async def fake_send(s):
        sent.append(s)
        return (s, 1, True)
    result = await daily_checkin.maybe_run_daily_checkin(fake_send)
    assert result is False
    assert sent == []


@pytest.mark.asyncio
async def test_fires_at_default_time_sends_question(monkeypatch):
    from agents import daily_checkin
    from storage import db
    monkeypatch.setattr(daily_checkin, "_now_local", lambda: _at_target())
    monkeypatch.setattr(daily_checkin, "compose_checkin_question",
                        AsyncMock(return_value="morning. emails? calendar?"))
    sent: list[str] = []
    async def fake_send(s):
        sent.append(s)
        return (s, 42, True)
    result = await daily_checkin.maybe_run_daily_checkin(fake_send)
    assert result is True
    assert sent == ["morning. emails? calendar?"]
    # Dedup marker set
    assert db.runtime_get("daily_checkin_last_fired_date") == "2026-05-21"
    # Pending-reply marker set
    assert db.runtime_get("daily_checkin_pending") is not None


@pytest.mark.asyncio
async def test_consume_pending_reply_both_yes(monkeypatch):
    from agents import daily_checkin
    from storage import db
    db.runtime_set("daily_checkin_pending",
                   datetime.now(UTC).isoformat())
    monkeypatch.setattr(daily_checkin, "fetch_email_buckets", AsyncMock(return_value={
        "unread_personal": [],
        "calendar_invites": [],
        "deletable": {"count": 10, "top_senders": ["linkedin.com"], "sample_ids": ["a", "b"]},
    }))
    monkeypatch.setattr(daily_checkin, "fetch_calendar_events", AsyncMock(return_value=[
        {"id": "ev1", "title": "standup", "start_iso": "", "end_iso": "",
         "location": "", "attendees_count": 0, "is_new_since_yesterday": False},
    ]))
    monkeypatch.setattr(daily_checkin, "compose_email_message",
                        AsyncMock(return_value="10 promos. nuke them?"))
    monkeypatch.setattr(daily_checkin, "compose_calendar_message",
                        AsyncMock(return_value="14:00 standup."))
    sent: list[str] = []
    async def fake_send(s):
        sent.append(s)
        return (s, 1, True)
    consumed = await daily_checkin.consume_pending_reply("yes", fake_send)
    assert consumed is True
    assert sent == ["10 promos. nuke them?", "14:00 standup."]
    # Pending cleared
    assert db.runtime_get("daily_checkin_pending") is None


@pytest.mark.asyncio
async def test_consume_pending_reply_no_clears_silently(monkeypatch):
    from agents import daily_checkin
    from storage import db
    db.runtime_set("daily_checkin_pending", datetime.now(UTC).isoformat())
    sent: list[str] = []
    async def fake_send(s):
        sent.append(s)
        return (s, 1, True)
    consumed = await daily_checkin.consume_pending_reply("no", fake_send)
    assert consumed is True
    assert sent == []  # silent ack — no chatter
    assert db.runtime_get("daily_checkin_pending") is None


@pytest.mark.asyncio
async def test_consume_pending_reply_ambiguous_does_not_consume(monkeypatch):
    from agents import daily_checkin
    from storage import db
    db.runtime_set("daily_checkin_pending", datetime.now(UTC).isoformat())
    sent: list[str] = []
    async def fake_send(s):
        sent.append(s)
        return (s, 1, True)
    consumed = await daily_checkin.consume_pending_reply("tell me a story", fake_send)
    assert consumed is False  # ambiguous → not a check-in reply, route normally
    # Pending stays — user might reply on their next message
    assert db.runtime_get("daily_checkin_pending") is not None


@pytest.mark.asyncio
async def test_consume_pending_reply_expired_window(monkeypatch):
    from agents import daily_checkin
    from storage import db
    # Window is 30 min by config; set pending 1h ago
    stale = (datetime.now(UTC) - timedelta(minutes=60)).isoformat()
    db.runtime_set("daily_checkin_pending", stale)
    sent: list[str] = []
    async def fake_send(s):
        sent.append(s)
        return (s, 1, True)
    consumed = await daily_checkin.consume_pending_reply("yes", fake_send)
    assert consumed is False  # window expired → not a check-in reply
    # Pending cleared by the expiry sweep
    assert db.runtime_get("daily_checkin_pending") is None


@pytest.mark.asyncio
async def test_consume_pending_reply_email_only(monkeypatch):
    from agents import daily_checkin
    from storage import db
    db.runtime_set("daily_checkin_pending", datetime.now(UTC).isoformat())
    monkeypatch.setattr(daily_checkin, "fetch_email_buckets", AsyncMock(return_value={
        "unread_personal": [], "calendar_invites": [],
        "deletable": {"count": 0, "top_senders": [], "sample_ids": []},
    }))
    cal_mock = AsyncMock()
    monkeypatch.setattr(daily_checkin, "fetch_calendar_events", cal_mock)
    monkeypatch.setattr(daily_checkin, "compose_email_message",
                        AsyncMock(return_value="inbox is quiet."))
    sent: list[str] = []
    async def fake_send(s):
        sent.append(s)
        return (s, 1, True)
    consumed = await daily_checkin.consume_pending_reply("just email", fake_send)
    assert consumed is True
    assert sent == ["inbox is quiet."]
    cal_mock.assert_not_called()  # calendar fetch skipped


@pytest.mark.asyncio
async def test_no_pending_means_not_consumed():
    from agents import daily_checkin
    sent: list[str] = []
    async def fake_send(s):
        sent.append(s)
        return (s, 1, True)
    consumed = await daily_checkin.consume_pending_reply("yes", fake_send)
    assert consumed is False
