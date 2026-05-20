"""Defensive subagent fetches: garbage in (401, malformed YAML) → empty out."""
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
async def test_fetch_email_buckets_happy_path(monkeypatch):
    from agents import daily_checkin
    yaml_body = (
        "unread_personal:\n"
        "  - {id: '1', from: 'mom@x.com', subject: 'call me', snippet: '...'}\n"
        "calendar_invites:\n"
        "  - {id: '2', from: 'cal@noreply', subject: 'standup'}\n"
        "deletable:\n"
        "  count: 28\n"
        "  top_senders: ['linkedin.com', 'spotify.com', 'uber.com']\n"
        "  sample_ids: ['p1', 'p2', 'p3']\n"
    )
    monkeypatch.setattr(daily_checkin, "run_internal_control",
                        AsyncMock(return_value=yaml_body))
    result = await daily_checkin.fetch_email_buckets()
    assert len(result["unread_personal"]) == 1
    assert result["unread_personal"][0]["from"] == "mom@x.com"
    assert result["deletable"]["count"] == 28
    assert result["deletable"]["sample_ids"] == ["p1", "p2", "p3"]


@pytest.mark.asyncio
async def test_fetch_email_buckets_401_returns_empty(monkeypatch):
    from agents import daily_checkin
    monkeypatch.setattr(daily_checkin, "run_internal_control", AsyncMock(
        return_value="Failed to authenticate. API Error: 401 ...",
    ))
    result = await daily_checkin.fetch_email_buckets()
    assert result == {"unread_personal": [], "calendar_invites": [],
                      "deletable": {"count": 0, "top_senders": [], "sample_ids": []}}


@pytest.mark.asyncio
async def test_fetch_email_buckets_malformed_yaml(monkeypatch):
    from agents import daily_checkin
    monkeypatch.setattr(daily_checkin, "run_internal_control", AsyncMock(
        return_value="not: valid: [yaml",
    ))
    result = await daily_checkin.fetch_email_buckets()
    assert result["deletable"]["count"] == 0


@pytest.mark.asyncio
async def test_fetch_email_buckets_exception(monkeypatch):
    from agents import daily_checkin
    monkeypatch.setattr(daily_checkin, "run_internal_control",
                        AsyncMock(side_effect=RuntimeError("boom")))
    result = await daily_checkin.fetch_email_buckets()
    assert result["deletable"]["count"] == 0


@pytest.mark.asyncio
async def test_fetch_email_caps_sample_ids(monkeypatch):
    """The cap (config: daily_checkin.max_delete_ids) is enforced."""
    from agents import daily_checkin
    ids = [f"id{i}" for i in range(500)]
    yaml_body = (
        "unread_personal: []\n"
        "calendar_invites: []\n"
        "deletable:\n"
        "  count: 500\n"
        "  top_senders: []\n"
        f"  sample_ids: {ids!r}\n"
    )
    monkeypatch.setattr(daily_checkin, "run_internal_control",
                        AsyncMock(return_value=yaml_body))
    result = await daily_checkin.fetch_email_buckets()
    assert len(result["deletable"]["sample_ids"]) == 200  # default cap


@pytest.mark.asyncio
async def test_fetch_calendar_happy_path(monkeypatch):
    from agents import daily_checkin
    yaml_body = (
        "events:\n"
        "  - {id: 'ev1', title: 'standup', start_iso: '2026-05-21T14:00:00+02:00',"
        "     end_iso: '2026-05-21T14:30:00+02:00', location: '', attendees_count: 4}\n"
    )
    monkeypatch.setattr(daily_checkin, "run_internal_control",
                        AsyncMock(return_value=yaml_body))
    events = await daily_checkin.fetch_calendar_events()
    assert len(events) == 1
    assert events[0]["title"] == "standup"
    assert events[0]["is_new_since_yesterday"] is True  # no prior list → all new


@pytest.mark.asyncio
async def test_fetch_calendar_new_event_detection(monkeypatch):
    from agents import daily_checkin
    from storage import db
    db.runtime_set("calendar_last_known_event_ids", '["ev1"]')
    yaml_body = (
        "events:\n"
        "  - {id: 'ev1', title: 'standup', start_iso: '2026-05-21T14:00:00+02:00',"
        "     end_iso: '2026-05-21T14:30:00+02:00'}\n"
        "  - {id: 'ev2', title: 'dr. visit', start_iso: '2026-05-21T16:30:00+02:00',"
        "     end_iso: '2026-05-21T17:00:00+02:00'}\n"
    )
    monkeypatch.setattr(daily_checkin, "run_internal_control",
                        AsyncMock(return_value=yaml_body))
    events = await daily_checkin.fetch_calendar_events()
    by_id = {e["id"]: e for e in events}
    assert by_id["ev1"]["is_new_since_yesterday"] is False
    assert by_id["ev2"]["is_new_since_yesterday"] is True
    # Side effect: known IDs updated
    import json
    raw = db.runtime_get("calendar_last_known_event_ids")
    assert raw is not None
    assert set(json.loads(raw)) == {"ev1", "ev2"}
