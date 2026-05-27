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


# ---------------------------------------------------------------------------
# Phase 15: action-mode reminder firing
# ---------------------------------------------------------------------------

def _insert_action_row(*, max_fires=3, minutes_ago=5, recurrence="every_n_minutes:20"):
    past = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()
    return db.reminder_insert(
        fire_at=past,
        text="autonomous notion write",
        kind="action",
        seed_prompt="write the next row",
        max_fires=max_fires,
        recurrence_rule=recurrence,
    )


@pytest.mark.asyncio
async def test_action_reminder_success_advances_fires_done(monkeypatch):
    rid = _insert_action_row(max_fires=3)
    from agents import proactive
    from agents import runtime as _rt
    import agents.engagement.guard as _guard
    monkeypatch.setattr(_guard, "should_wake", lambda source_id=None: True)

    async def fake_run(seed_prompt, **_kwargs):
        return ""

    monkeypatch.setattr(_rt, "run_scheduled_action", fake_run)

    sent = []
    async def fake_send(s): sent.append(s)

    await proactive.fire_due_reminders(fake_send)
    row = db.reminder_get(rid)
    assert row["fires_done"] == 1
    assert row["status"] == "active"
    # No push to user during the inner work — only summary or failure surfaces.
    assert sent == []


@pytest.mark.asyncio
async def test_action_reminder_failure_increments_counter_not_cancelled(monkeypatch):
    rid = _insert_action_row(max_fires=3)
    from agents import proactive
    from agents import runtime as _rt
    import agents.engagement.guard as _guard
    monkeypatch.setattr(_guard, "should_wake", lambda source_id=None: True)

    async def fake_run(seed_prompt, **_kwargs):
        raise RuntimeError("simulated MCP timeout")

    monkeypatch.setattr(_rt, "run_scheduled_action", fake_run)

    async def fake_send(s): pass

    await proactive.fire_due_reminders(fake_send)
    row = db.reminder_get(rid)
    assert row["consecutive_failures"] == 1
    assert row["fires_done"] == 0
    assert row["status"] == "active"


@pytest.mark.asyncio
async def test_action_reminder_three_strikes_cancels_and_surfaces(monkeypatch):
    rid = _insert_action_row(max_fires=10)
    db.reminder_increment_failures(rid)
    db.reminder_increment_failures(rid)   # pre-load to 2 failures

    from agents import proactive
    from agents import runtime as _rt
    import agents.engagement.guard as _guard
    monkeypatch.setattr(_guard, "should_wake", lambda source_id=None: True)

    async def fake_run(seed_prompt, **_kwargs):
        raise RuntimeError("third strike")

    monkeypatch.setattr(_rt, "run_scheduled_action", fake_run)

    # Capture text that would have been pushed via reserve_and_send.
    surfaced = []
    async def fake_reserve(*, send_text_fn, text, **_):
        surfaced.append(text)
        class _R: status = "sent"; reason = None
        return _R()

    monkeypatch.setattr(proactive, "reserve_and_send", fake_reserve)

    async def fake_send(s): pass

    await proactive.fire_due_reminders(fake_send)
    row = db.reminder_get(rid)
    assert row["status"] == "cancelled"
    assert row["consecutive_failures"] == 3
    assert any("cancelled" in s and "3 failures" in s for s in surfaced)


@pytest.mark.asyncio
async def test_action_reminder_last_fire_marks_fired_and_runs_summary(monkeypatch):
    rid = db.reminder_insert(
        fire_at=(datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
        text="x", kind="action",
        seed_prompt="write a row",
        summary_prompt="wrap it up",
        max_fires=2,
        recurrence_rule="every_n_minutes:20",
    )
    # Pre-advance to 1/2 so this fire is the FINAL one.
    db.reminder_increment_fires_done(rid)

    from agents import proactive
    from agents import runtime as _rt
    import agents.engagement.guard as _guard
    monkeypatch.setattr(_guard, "should_wake", lambda source_id=None: True)

    calls = []

    async def fake_run(seed_prompt, **_kwargs):
        calls.append(seed_prompt)
        return "summary text from hikari" if "wrap it up" in seed_prompt else ""

    monkeypatch.setattr(_rt, "run_scheduled_action", fake_run)

    surfaced = []
    async def fake_reserve(*, text, **_):
        surfaced.append(text)
        class _R: status = "sent"; reason = None
        return _R()

    monkeypatch.setattr(proactive, "reserve_and_send", fake_reserve)

    async def fake_send(s): pass

    await proactive.fire_due_reminders(fake_send)

    row = db.reminder_get(rid)
    assert row["status"] == "fired", "last fire of bounded schedule must close the row"
    assert row["fires_done"] == 2
    # The summary turn ran and its text was pushed.
    assert any("wrap it up" in c for c in calls)
    assert any("summary text from hikari" in s for s in surfaced)


@pytest.mark.asyncio
async def test_action_reminder_user_turn_in_progress_defers(monkeypatch):
    rid = _insert_action_row(max_fires=3, minutes_ago=5)
    original_due = db.reminder_get(rid)["fire_at"]
    from agents import proactive
    from agents import runtime as _rt
    import agents.engagement.guard as _guard
    monkeypatch.setattr(_guard, "should_wake", lambda source_id=None: True)

    # Hold the run-lock to simulate an active user turn.
    await _rt._RUN_LOCK.acquire()
    try:
        called = {"n": 0}

        async def fake_run(*_a, **_k):
            called["n"] += 1
            return ""

        monkeypatch.setattr(_rt, "run_scheduled_action", fake_run)
        async def fake_send(s): pass
        await proactive.fire_due_reminders(fake_send)
    finally:
        _rt._RUN_LOCK.release()

    assert called["n"] == 0, "action turn must not preempt a user turn"
    row = db.reminder_get(rid)
    assert row["fire_at"] != original_due, "fire_at must be deferred"
    assert row["fires_done"] == 0
    assert row["status"] == "active"


@pytest.mark.asyncio
async def test_text_reminder_unchanged_by_action_branch(monkeypatch):
    """Existing text-reminder behaviour must be untouched by the new
    action branch — same emoji prefix, same status flip."""
    sent: list[str] = []
    async def fake_send(s): sent.append(s)
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    rid = db.reminder_insert(fire_at=past, text="text-mode", lead_minutes=0)
    from agents import proactive
    await proactive.fire_due_reminders(fake_send)
    assert sent, "text reminder should still push"
    assert sent[0].startswith("⏰")
    assert db.reminder_get(rid)["status"] == "fired"
