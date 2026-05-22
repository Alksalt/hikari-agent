"""Voice composition: build per-topic Hikari messages with SDK-error guard."""
from __future__ import annotations

import importlib
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


@pytest.mark.asyncio
async def test_compose_email_returns_voice_text(monkeypatch):
    from agents import daily_checkin
    mock = AsyncMock(return_value="three actual emails. 28 promos. want me to nuke them?")
    monkeypatch.setattr(daily_checkin, "run_visible_proactive", mock)
    data = {
        "unread_personal": [{"id": "1", "from": "mom@x.com", "subject": "call me", "snippet": "..."}],
        "calendar_invites": [],
        "deletable": {"count": 28, "top_senders": ["linkedin.com"], "sample_ids": ["a", "b"]},
    }
    text = await daily_checkin.compose_email_message(data)
    assert "promos" in text or "emails" in text
    # The delete proposal is now unconditional when count > 0.
    prompt = mock.call_args[0][0]
    assert "deletable: 28" in prompt
    assert "in promos/updates" in prompt


@pytest.mark.asyncio
async def test_compose_email_no_delete_proposal_when_zero(monkeypatch):
    """If deletable.count is 0, the conditional delete_line must NOT be in
    the prompt. (The rules section's mention of 'deletable > 0' is constant;
    we only check the data-driven 'in promos/updates' phrase from delete_line.)"""
    from agents import daily_checkin
    mock = AsyncMock(return_value="inbox is quiet.")
    monkeypatch.setattr(daily_checkin, "run_visible_proactive", mock)
    data = {
        "unread_personal": [],
        "calendar_invites": [],
        "deletable": {"count": 0, "top_senders": [], "sample_ids": []},
    }
    await daily_checkin.compose_email_message(data)
    prompt = mock.call_args[0][0]
    assert "in promos/updates" not in prompt
    assert "deletable: 0" not in prompt


@pytest.mark.asyncio
async def test_compose_email_sdk_error_returns_none(monkeypatch):
    from agents import daily_checkin
    monkeypatch.setattr(daily_checkin, "run_visible_proactive",
                        AsyncMock(return_value="Failed to authenticate. API Error: 401 ..."))
    data = {"unread_personal": [], "calendar_invites": [],
            "deletable": {"count": 0, "top_senders": [], "sample_ids": []}}
    text = await daily_checkin.compose_email_message(data)
    assert text is None


@pytest.mark.asyncio
async def test_compose_email_no_message_returns_none(monkeypatch):
    from agents import daily_checkin
    monkeypatch.setattr(daily_checkin, "run_visible_proactive",
                        AsyncMock(return_value="NO_MESSAGE"))
    text = await daily_checkin.compose_email_message({
        "unread_personal": [], "calendar_invites": [],
        "deletable": {"count": 0, "top_senders": [], "sample_ids": []},
    })
    assert text is None


@pytest.mark.asyncio
async def test_compose_calendar_returns_voice_text(monkeypatch):
    from agents import daily_checkin
    monkeypatch.setattr(daily_checkin, "run_visible_proactive",
                        AsyncMock(return_value="14:00 standup. 16:30 dr. visit."))
    events = [
        {"id": "ev1", "title": "standup", "start_iso": "2026-05-21T14:00:00+02:00",
         "end_iso": "2026-05-21T14:30:00+02:00", "location": "", "attendees_count": 4,
         "is_new_since_yesterday": False},
    ]
    text = await daily_checkin.compose_calendar_message(events)
    assert "standup" in text


@pytest.mark.asyncio
async def test_compose_checkin_question_returns_voice_text(monkeypatch):
    from agents import daily_checkin
    monkeypatch.setattr(daily_checkin, "run_visible_proactive",
                        AsyncMock(return_value="morning. check emails? check calendar?"))
    text = await daily_checkin.compose_checkin_question()
    assert "email" in text.lower()
    assert "calendar" in text.lower()
