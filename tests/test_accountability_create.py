"""Tests for accountability_create and accountability_resolve tools."""
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


@pytest.mark.asyncio
async def test_happy_path_creates_two_reminders_and_item():
    from tools.reminders.accountability import accountability_create
    fire_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    out = await accountability_create.handler({
        "when_iso": fire_at,
        "task_text": "drink water",
        "check_after_minutes": 60,
    })
    assert "data" in out
    data = out["data"]
    rid = data["reminder_id"]
    follow_rid = data["follow_up_reminder_id"]
    item_id = data["id"]

    # Both reminders exist and are active.
    r = db.reminder_get(rid)
    assert r is not None
    assert r["text"] == "drink water"
    assert r["status"] == "active"

    fr = db.reminder_get(follow_rid)
    assert fr is not None
    assert fr["status"] == "active"
    # Follow-up fires 60 minutes after the primary.
    primary_dt = datetime.fromisoformat(r["fire_at"])
    followup_dt = datetime.fromisoformat(fr["fire_at"])
    delta_min = (followup_dt - primary_dt).total_seconds() / 60
    assert abs(delta_min - 60) < 1

    # Accountability item links them.
    item = db.accountability_get(item_id)
    assert item is not None
    assert item["reminder_id"] == rid
    assert item["follow_up_reminder_id"] == follow_rid
    assert item["task_text"] == "drink water"
    assert item["outcome"] is None


@pytest.mark.asyncio
async def test_default_check_after_minutes_is_180():
    from tools.reminders.accountability import accountability_create
    fire_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    out = await accountability_create.handler({
        "when_iso": fire_at,
        "task_text": "stretch",
    })
    data = out["data"]
    r = db.reminder_get(data["reminder_id"])
    fr = db.reminder_get(data["follow_up_reminder_id"])
    primary_dt = datetime.fromisoformat(r["fire_at"])
    followup_dt = datetime.fromisoformat(fr["fire_at"])
    delta_min = (followup_dt - primary_dt).total_seconds() / 60
    assert abs(delta_min - 180) < 1


@pytest.mark.asyncio
async def test_empty_task_text_refused():
    from tools.reminders.accountability import accountability_create
    fire_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    out = await accountability_create.handler({"when_iso": fire_at, "task_text": ""})
    assert "refused" in out["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_bad_iso_refused():
    from tools.reminders.accountability import accountability_create
    out = await accountability_create.handler({
        "when_iso": "not-a-timestamp",
        "task_text": "exercise",
    })
    assert "refused" in out["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_primary_in_past_refused():
    from tools.reminders.accountability import accountability_create
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    out = await accountability_create.handler({
        "when_iso": past,
        "task_text": "run",
    })
    assert "refused" in out["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_check_after_minutes_too_small_refused():
    from tools.reminders.accountability import accountability_create
    fire_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    out = await accountability_create.handler({
        "when_iso": fire_at,
        "task_text": "breathe",
        "check_after_minutes": 1,
    })
    assert "refused" in out["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_check_after_minutes_too_large_refused():
    from tools.reminders.accountability import accountability_create
    fire_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    out = await accountability_create.handler({
        "when_iso": fire_at,
        "task_text": "sleep",
        "check_after_minutes": 1440 * 8,
    })
    assert "refused" in out["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_followup_has_no_calendar_sync():
    from tools.reminders.accountability import accountability_create
    fire_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    out = await accountability_create.handler({
        "when_iso": fire_at,
        "task_text": "journal",
        "check_after_minutes": 30,
    })
    data = out["data"]
    fr = db.reminder_get(data["follow_up_reminder_id"])
    assert fr["gcal_sync_pending"] == 0
    assert fr["apple_sync_pending"] == 0
