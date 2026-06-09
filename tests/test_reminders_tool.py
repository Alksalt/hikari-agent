"""Phase 10: reminder MCP tool surface."""
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
    """Default call (include_done=False) returns only active reminders."""
    from tools import reminders
    fire = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    r1 = await reminders.reminder_create.handler({"when_iso": fire, "text": "A"})
    r2 = await reminders.reminder_create.handler({"when_iso": fire, "text": "B"})
    out = await reminders.reminder_list.handler({})
    ids = [r["id"] for r in out["data"]["reminders"]]
    assert r1["data"]["id"] in ids
    assert r2["data"]["id"] in ids
    assert len(ids) == 2


@pytest.mark.asyncio
async def test_reminder_list_include_done_shows_fired():
    """include_done=True also returns fired/cancelled reminders."""
    from tools import reminders
    fire = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    r1 = await reminders.reminder_create.handler({"when_iso": fire, "text": "active one"})
    r2 = await reminders.reminder_create.handler({"when_iso": fire, "text": "done one"})
    rid2 = r2["data"]["id"]
    # Mark the second reminder as cancelled via db layer to simulate fired state
    db.reminder_cancel(rid2)
    # Default: only active returned
    out_default = await reminders.reminder_list.handler({})
    default_ids = [r["id"] for r in out_default["data"]["reminders"]]
    assert r1["data"]["id"] in default_ids
    assert rid2 not in default_ids
    # include_done=True: both returned
    out_all = await reminders.reminder_list.handler({"include_done": True})
    all_ids = [r["id"] for r in out_all["data"]["reminders"]]
    assert r1["data"]["id"] in all_ids
    assert rid2 in all_ids

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


# ---------------------------------------------------------------------------
# Phase 15: action-mode reminders
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_action_reminder_happy_path():
    from tools import reminders
    fire = (datetime.now(UTC) + timedelta(minutes=20)).isoformat()
    out = await reminders.reminder_create.handler({
        "when_iso": fire, "text": "autonomous notion write",
        "kind": "action",
        "recurrence": "every_n_minutes:20",
        "max_fires": 6,
        "seed_prompt": "write the next row to notion db abc123",
        "summary_prompt": "summarize what you wrote",
        "budget_usd_per_fire": 0.40,
        "timeout_s": 180,
    })
    assert "data" in out
    assert out["data"]["kind"] == "action"
    rid = out["data"]["id"]
    row = db.reminder_get(rid)
    assert row["kind"] == "action"
    assert row["seed_prompt"] == "write the next row to notion db abc123"
    assert row["max_fires"] == 6
    assert row["recurrence_rule"] == "every_n_minutes:20"
    # Calendar mirroring is forced off for action kind.
    assert row["gcal_sync_pending"] == 0
    assert row["apple_sync_pending"] == 0


@pytest.mark.asyncio
async def test_action_reminder_missing_seed_prompt_refused():
    from tools import reminders
    fire = (datetime.now(UTC) + timedelta(minutes=20)).isoformat()
    out = await reminders.reminder_create.handler({
        "when_iso": fire, "text": "x", "kind": "action",
        "recurrence": "every_n_minutes:20", "max_fires": 6,
    })
    assert "refused" in out["content"][0]["text"]
    assert "seed_prompt" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_action_reminder_missing_recurrence_refused():
    from tools import reminders
    fire = (datetime.now(UTC) + timedelta(minutes=20)).isoformat()
    out = await reminders.reminder_create.handler({
        "when_iso": fire, "text": "x", "kind": "action",
        "seed_prompt": "do it", "max_fires": 6,
    })
    assert "refused" in out["content"][0]["text"]
    assert "recurrence" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_action_reminder_missing_max_fires_refused():
    from tools import reminders
    fire = (datetime.now(UTC) + timedelta(minutes=20)).isoformat()
    out = await reminders.reminder_create.handler({
        "when_iso": fire, "text": "x", "kind": "action",
        "seed_prompt": "do it", "recurrence": "every_n_minutes:20",
    })
    assert "refused" in out["content"][0]["text"]
    assert "max_fires" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_action_reminder_cost_cap_refused():
    """A schedule of 100 fires × $0.40 = $40 must refuse (cap is $5)."""
    from tools import reminders
    fire = (datetime.now(UTC) + timedelta(minutes=20)).isoformat()
    out = await reminders.reminder_create.handler({
        "when_iso": fire, "text": "x", "kind": "action",
        "seed_prompt": "do it", "recurrence": "every_n_minutes:20",
        "max_fires": 100,
    })
    assert "refused" in out["content"][0]["text"]
    body = out["content"][0]["text"]
    assert "exceeds cap" in body or "total budget" in body


@pytest.mark.asyncio
async def test_action_reminder_invalid_kind_refused():
    from tools import reminders
    fire = (datetime.now(UTC) + timedelta(minutes=20)).isoformat()
    out = await reminders.reminder_create.handler({
        "when_iso": fire, "text": "x", "kind": "weird",
    })
    assert "refused" in out["content"][0]["text"]
    assert "kind" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_text_reminder_unchanged_by_action_args():
    """Existing text reminder behaviour must be unaffected when action-mode
    args are absent."""
    from tools import reminders
    fire = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    out = await reminders.reminder_create.handler({
        "when_iso": fire, "text": "old-school ping",
    })
    rid = out["data"]["id"]
    row = db.reminder_get(rid)
    assert row["kind"] == "text"
    assert row["seed_prompt"] is None
    assert row["max_fires"] is None
