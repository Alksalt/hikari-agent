"""Tests for fire_due_reminders accountability branch."""
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
    import agents.proactive_gate as _gate
    monkeypatch.setattr(_gate, "_is_quiet_now", lambda _db=None: False)
    monkeypatch.setattr(_gate, "_silence_active", lambda _db: False)


def _insert_due_reminder(text: str = "task") -> int:
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    return db.reminder_insert(fire_at=past, text=text, lead_minutes=0, repeat=None)


@pytest.mark.asyncio
async def test_primary_reminder_fires_with_emoji(monkeypatch):
    """Primary reminder (non-accountability) fires with ⏰ prefix."""
    sent: list[str] = []
    async def fake_send(s: str): sent.append(s)

    _insert_due_reminder("drink water")
    from agents import proactive
    await proactive.fire_due_reminders(fake_send)
    assert sent
    assert sent[0].startswith("⏰")
    assert "drink water" in sent[0]


@pytest.mark.asyncio
async def test_followup_fires_with_aux_llm_text(monkeypatch):
    """When a follow-up reminder is due, it fires with aux-LLM text and no ⏰."""
    sent: list[str] = []
    async def fake_send(s: str): sent.append(s)

    # Insert an accountability item with a follow-up that is already due.
    primary_fire = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    followup_fire = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    rid = db.reminder_insert(fire_at=primary_fire, text="drink water", lead_minutes=0)
    follow_rid = db.reminder_insert(fire_at=followup_fire, text="drink water", lead_minutes=0)
    item_id = db.accountability_insert(rid, follow_rid, "drink water")

    # Monkeypatch aux-LLM to return fixed string.
    import agents.proactive as _proactive
    async def fake_aux_llm(task_text: str) -> str:
        return "so. water?"
    monkeypatch.setattr(_proactive, "_generate_accountability_followup_text", fake_aux_llm)

    await _proactive.fire_due_reminders(fake_send)

    assert sent
    assert sent[0] == "so. water?"
    assert not sent[0].startswith("⏰"), "follow-up should NOT start with ⏰"
    # pending_accountability_check should be set.
    assert db.runtime_get("pending_accountability_check") == str(item_id)


@pytest.mark.asyncio
async def test_followup_falls_back_when_aux_llm_raises(monkeypatch):
    """If aux-LLM raises, fire_due_reminders uses the fallback line instead."""
    sent: list[str] = []
    async def fake_send(s: str): sent.append(s)

    primary_fire = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    followup_fire = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    rid = db.reminder_insert(fire_at=primary_fire, text="meditate", lead_minutes=0)
    follow_rid = db.reminder_insert(fire_at=followup_fire, text="meditate", lead_minutes=0)
    db.accountability_insert(rid, follow_rid, "meditate")

    import agents.runtime as _rt
    async def exploding_aux_llm(*a, **kw):
        raise RuntimeError("openrouter down")
    monkeypatch.setattr(_rt, "_call_aux_llm", exploding_aux_llm)

    from agents import proactive as _p
    await _p.fire_due_reminders(fake_send)

    assert sent
    assert "meditate" in sent[0]
    assert not sent[0].startswith("⏰")


@pytest.mark.asyncio
async def test_already_resolved_followup_fires_as_plain_reminder(monkeypatch):
    """If the accountability outcome is already set, fire as plain ⏰ reminder."""
    sent: list[str] = []
    async def fake_send(s: str): sent.append(s)

    primary_fire = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    followup_fire = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    rid = db.reminder_insert(fire_at=primary_fire, text="exercise", lead_minutes=0)
    follow_rid = db.reminder_insert(fire_at=followup_fire, text="exercise", lead_minutes=0)
    item_id = db.accountability_insert(rid, follow_rid, "exercise")
    # Pre-resolve it.
    db.accountability_resolve(item_id, 1)

    from agents import proactive as _p
    await _p.fire_due_reminders(fake_send)

    assert sent
    assert sent[0].startswith("⏰"), f"expected ⏰ prefix, got: {sent[0]!r}"
    # pending key must NOT be set for an already-resolved item.
    assert db.runtime_get("pending_accountability_check") is None
