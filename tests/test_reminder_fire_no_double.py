"""reminder_fire producer enabled as SILENT AWARENESS — never a second firing path.

Ownership contract (sprint-9 single-owner decision, upheld):
- ``fire_due_reminders`` (agents/proactive.py) is the SOLE firing path —
  dedicated 60s job, proactive-disabled exemption, recurrence, keyboards.
- The ``reminder_fire`` producer is enabled with ``send_mode: silent`` so the
  selector can SEE a due reminder (and hold back competing proactive pings
  inside the suppression window) but can never SEND it.
"""
from __future__ import annotations

import importlib
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from agents import config
    config.reload()
    yield
    config.reload()


@pytest.fixture(autouse=True)
def _gate_open(monkeypatch):
    import agents.proactive_gate as _gate
    monkeypatch.setattr(_gate, "_is_quiet_now", lambda _db=None: False)
    monkeypatch.setattr(_gate, "_silence_active", lambda _db: False)


def _ctx(enabled: set[str]) -> SimpleNamespace:
    return SimpleNamespace(
        now_local=datetime.now(UTC),
        mood="focused",
        enabled_sources=enabled,
        pool_caps={"user_anchored": True, "agent_spontaneous": True,
                   "scheduled_ceremony": False},
        source_response_rate={},
        last_send_per_source={},
    )


def _reminder_candidate(rid: int, fire_at_iso: str):
    from agents.engagement.triggers import TriggerCandidate
    return TriggerCandidate(
        source="reminder_fire",
        pool="user_anchored",
        pattern="notify",
        novelty=0.9,
        actionability=1.0,
        confidence=1.0,
        payload={"text": "stretch", "id": rid, "fire_at": fire_at_iso},
        dedup_key=f"reminder:{rid}",
        decay_at=datetime.now(UTC) + timedelta(hours=1),
    )


def _calendar_candidate():
    from agents.engagement.triggers import TriggerCandidate
    return TriggerCandidate(
        source="calendar_event_prep",
        pool="user_anchored",
        pattern="notify",
        novelty=0.8,
        actionability=0.8,
        confidence=0.9,
        payload={"title": "standup", "minutes_until": 30},
        dedup_key="cal:1",
        decay_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.mark.asyncio
async def test_no_double_fire_producer_is_silent():
    """Owner fires exactly once; the producer's candidate is never selected."""
    from agents import proactive
    from agents.engagement import selector
    from agents.engagement.producers import reminder_fire
    from storage import db

    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    rid = db.reminder_insert(fire_at=past, text="stretch", lead_minutes=0, repeat=None)

    # Producer sees the due reminder before the owner fires it.
    candidates = reminder_fire.collect()
    assert candidates, "producer must surface the due reminder when enabled"
    assert all(c.dedup_key == f"reminder:{rid}" for c in candidates), (
        "producer dedup_key must live in the owner's reminder: namespace"
    )

    # Selector must never pick the silent reminder candidate for sending.
    got = selector.select(candidates, _ctx({"reminder_fire"}))
    assert got is None, "silent reminder_fire candidate must never be selected"

    # The owner fires it exactly once.
    sent: list[str] = []

    async def fake_send(text):
        sent.append(text)
        return text, 42, True

    fired = await proactive.fire_due_reminders(fake_send)
    assert fired == 1
    assert len(sent) == 1

    # After firing, the producer no longer surfaces it — nothing left to race.
    assert reminder_fire.collect() == []


@pytest.mark.asyncio
async def test_proactive_disabled_owner_still_fires():
    """Global proactive off: user reminders still fire (the one exemption)."""
    from agents import proactive
    from storage import db

    db.runtime_set("proactive_enabled_sources_override", "[]")
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    db.reminder_insert(fire_at=past, text="stretch", lead_minutes=0, repeat=None)

    sent: list[str] = []

    async def fake_send(text):
        sent.append(text)
        return text, 42, True

    fired = await proactive.fire_due_reminders(fake_send)
    assert fired == 1, "user-created reminders must fire even when proactive is off"
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_recurrence_stays_with_owner():
    """Recurring reminder: owner reschedules; row stays active, fire_at advances."""
    from agents import proactive
    from storage import db

    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    rid = db.reminder_insert(
        fire_at=past, text="daily stretch", lead_minutes=0, repeat=None,
        recurrence_rule="daily",
    )

    async def fake_send(text):
        return text, 42, True

    fired = await proactive.fire_due_reminders(fake_send)
    assert fired == 1
    row = db.reminder_get(rid)
    assert row is not None
    assert row["status"] == "active", "recurring reminder must stay active"
    new_fire = datetime.fromisoformat(row["fire_at"])
    if new_fire.tzinfo is None:
        new_fire = new_fire.replace(tzinfo=UTC)
    assert new_fire > datetime.now(UTC), "fire_at must be rescheduled into the future"


def _neutral_scoring(stack: ExitStack) -> None:
    """Neutralize config-driven scoring so only the suppression logic varies."""
    stack.enter_context(patch(
        "agents.engagement.selector._value_score", return_value=0.5))
    stack.enter_context(patch(
        "agents.engagement.selector._source_min_value_score", return_value=0.0))
    stack.enter_context(patch(
        "agents.engagement.selector._time_of_day_multiplier", return_value=1.0))


def test_suppression_holds_competing_ping():
    """A due reminder inside the window makes select() hold other candidates."""
    from agents.engagement import selector

    due_iso = datetime.now(UTC).isoformat()
    cands = [_reminder_candidate(7, due_iso), _calendar_candidate()]
    with ExitStack() as stack:
        _neutral_scoring(stack)
        got = selector.select(cands, _ctx({"reminder_fire", "calendar_event_prep"}))
    assert got is None, "competing ping must be held while a reminder is imminent"


def test_no_suppression_without_reminder():
    """Control: same calendar candidate is selected when no reminder is due."""
    from agents.engagement import selector

    with ExitStack() as stack:
        _neutral_scoring(stack)
        got = selector.select([_calendar_candidate()], _ctx({"calendar_event_prep"}))
    assert got is not None
    assert got.source == "calendar_event_prep"


def test_no_suppression_outside_window():
    """A reminder due far in the future does not suppress anything."""
    from agents.engagement import selector

    far_iso = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    cands = [_reminder_candidate(8, far_iso), _calendar_candidate()]
    with ExitStack() as stack:
        _neutral_scoring(stack)
        got = selector.select(cands, _ctx({"reminder_fire", "calendar_event_prep"}))
    assert got is not None
    assert got.source == "calendar_event_prep"


def test_producer_has_no_consume_or_fire_hooks():
    """The producer must never grow mark_consumed/mark_fired — no-send contract."""
    from agents.engagement.producers import reminder_fire

    assert not hasattr(reminder_fire, "mark_consumed")
    assert not hasattr(reminder_fire, "mark_fired")


def test_select_empty_candidates_still_none():
    """Sanity: the new suppression pre-scan must not crash on empty input."""
    from agents.engagement import selector

    assert selector.select([], _ctx({"reminder_fire"})) is None
