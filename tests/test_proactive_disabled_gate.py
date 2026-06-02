"""tests/test_proactive_disabled_gate.py — Phase 2: proactive.enabled=false gate.

Verifies that reserve_and_send:
  1. Aborts with reason="proactive_disabled" for ceremony/non-reminder producers
     when proactive_enabled_sources_override == "[]" (global off).
  2. Passes through for producer_id="reminder" even when globally off.
  3. Does not regress when override is NULL (defaults = ON).
  4. Does not regress when override is a non-empty list (specific sources ON).

Unit tests for the two helpers are also included.
"""
from __future__ import annotations

import importlib

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """Fresh DB + runtime_state isolated to this test."""
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    import storage.db as db_mod
    importlib.reload(db_mod)
    db_mod._reset_schema_sentinel()
    db_mod.get_session_id()
    yield db_mod


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


def test_proactive_globally_disabled_when_empty_list(isolated_db):
    isolated_db.runtime_set("proactive_enabled_sources_override", "[]")

    import agents.proactive_gate as pg
    importlib.reload(pg)
    assert pg._proactive_globally_disabled(isolated_db) is True


def test_proactive_globally_disabled_false_when_null(isolated_db):
    isolated_db.runtime_set("proactive_enabled_sources_override", None)

    import agents.proactive_gate as pg
    importlib.reload(pg)
    assert pg._proactive_globally_disabled(isolated_db) is False


def test_proactive_globally_disabled_false_when_populated_list(isolated_db):
    import json
    isolated_db.runtime_set(
        "proactive_enabled_sources_override",
        json.dumps(["gmail_unread_threshold"]),
    )

    import agents.proactive_gate as pg
    importlib.reload(pg)
    assert pg._proactive_globally_disabled(isolated_db) is False


def test_proactive_globally_disabled_fails_closed_on_read_error(isolated_db):
    """A runtime_get failure must fail CLOSED (treat as disabled — no leak)."""
    import agents.proactive_gate as pg
    importlib.reload(pg)

    class _BoomDB:
        def runtime_get(self, _key):
            raise RuntimeError("db unavailable")

    assert pg._proactive_globally_disabled(_BoomDB()) is True


def test_is_reminder_producer_true():
    from agents.proactive_gate import _is_reminder_producer
    assert _is_reminder_producer("reminder") is True


def test_is_reminder_producer_false_for_ceremony():
    from agents.proactive_gate import _is_reminder_producer
    assert _is_reminder_producer("morning_brief") is False
    assert _is_reminder_producer("decision_log") is False
    assert _is_reminder_producer("daily_checkin") is False


# ---------------------------------------------------------------------------
# reserve_and_send integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_morning_brief_aborted_when_globally_off(isolated_db, monkeypatch):
    """morning_brief producer is aborted with 'proactive_disabled' when off."""
    isolated_db.runtime_set("proactive_enabled_sources_override", "[]")

    import agents.proactive_gate as pg
    importlib.reload(pg)
    # Silence other gates so only proactive_disabled fires.
    monkeypatch.setattr(pg, "_is_quiet_now", lambda _db=None: False)

    send_called = []

    async def mock_send(text):
        send_called.append(text)
        return (text, 1, True)

    result = await pg.reserve_and_send(
        send_text_fn=mock_send,
        producer_id="morning_brief",
        pattern="brief",
        text="Good morning!",
        db=isolated_db,
    )

    assert result.status == "aborted"
    assert result.reason == "proactive_disabled"
    assert send_called == [], "send_text_fn must not be called"


@pytest.mark.asyncio
async def test_decision_log_aborted_when_globally_off(isolated_db, monkeypatch):
    """decision_log producer is aborted with 'proactive_disabled' when off."""
    isolated_db.runtime_set("proactive_enabled_sources_override", "[]")

    import agents.proactive_gate as pg
    importlib.reload(pg)
    monkeypatch.setattr(pg, "_is_quiet_now", lambda _db=None: False)

    send_called = []

    async def mock_send(text):
        send_called.append(text)
        return (text, 2, True)

    result = await pg.reserve_and_send(
        send_text_fn=mock_send,
        producer_id="decision_log",
        pattern="resolve_due",
        text="You have a decision to resolve.",
        db=isolated_db,
    )

    assert result.status == "aborted"
    assert result.reason == "proactive_disabled"
    assert send_called == []


@pytest.mark.asyncio
async def test_generic_ceremony_aborted_when_globally_off(isolated_db, monkeypatch):
    """A generic ceremony source is aborted when globally off."""
    isolated_db.runtime_set("proactive_enabled_sources_override", "[]")

    import agents.proactive_gate as pg
    importlib.reload(pg)
    monkeypatch.setattr(pg, "_is_quiet_now", lambda _db=None: False)

    send_called = []

    async def mock_send(text):
        send_called.append(text)
        return (text, 3, True)

    result = await pg.reserve_and_send(
        send_text_fn=mock_send,
        producer_id="future_letter",
        pattern="letter",
        text="A letter from the past.",
        db=isolated_db,
    )

    assert result.status == "aborted"
    assert result.reason == "proactive_disabled"
    assert send_called == []


@pytest.mark.asyncio
async def test_reminder_still_sends_when_globally_off(isolated_db, monkeypatch):
    """User-created reminders (producer_id='reminder') are exempt from the gate."""
    isolated_db.runtime_set("proactive_enabled_sources_override", "[]")

    import agents.proactive_gate as pg
    importlib.reload(pg)
    monkeypatch.setattr(pg, "_is_quiet_now", lambda _db=None: False)

    send_called = []

    async def mock_send(text):
        send_called.append(text)
        return (text, 99, True)

    result = await pg.reserve_and_send(
        send_text_fn=mock_send,
        producer_id="reminder",
        pattern="fire",
        text="⏰ Doctor appointment in 15 min",
        db=isolated_db,
    )

    assert result.status == "sent"
    assert result.reason is None
    assert send_called == ["⏰ Doctor appointment in 15 min"]


@pytest.mark.asyncio
async def test_no_regression_when_proactive_on_null(isolated_db, monkeypatch):
    """When override is NULL (defaults=ON), the new gate does not abort."""
    isolated_db.runtime_set("proactive_enabled_sources_override", None)

    import agents.proactive_gate as pg
    importlib.reload(pg)
    monkeypatch.setattr(pg, "_is_quiet_now", lambda _db=None: False)

    async def mock_send(text):
        return (text, 10, True)

    result = await pg.reserve_and_send(
        send_text_fn=mock_send,
        producer_id="morning_brief",
        pattern="brief",
        text="Good morning!",
        db=isolated_db,
    )

    assert result.status == "sent"
    assert result.reason is None


@pytest.mark.asyncio
async def test_no_regression_when_proactive_on_populated_list(isolated_db, monkeypatch):
    """When override is a non-empty list (specific sources ON), gate does not abort."""
    import json
    isolated_db.runtime_set(
        "proactive_enabled_sources_override",
        json.dumps(["morning_brief", "gmail_unread_threshold"]),
    )

    import agents.proactive_gate as pg
    importlib.reload(pg)
    monkeypatch.setattr(pg, "_is_quiet_now", lambda _db=None: False)

    async def mock_send(text):
        return (text, 11, True)

    result = await pg.reserve_and_send(
        send_text_fn=mock_send,
        producer_id="morning_brief",
        pattern="brief",
        text="Good morning!",
        db=isolated_db,
    )

    assert result.status == "sent"
    assert result.reason is None
