"""tests/test_proactive_gate.py — reason-contract population in proactive_gate.

Verifies that reserve_and_send correctly:
  1. Calls proactive_event_insert with reason-contract fields from candidate.
  2. Calls proactive_event_update_terminal with the same fields on success.
  3. Falls back gracefully when candidate is None (no error, no contract fields).
  4. _extract_reason_contract handles TriggerCandidate, dicts, and None.
"""
from __future__ import annotations

import importlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    import storage.db as db_mod
    importlib.reload(db_mod)
    db_mod._reset_schema_sentinel()
    db_mod.get_session_id()
    yield db_mod


# --- _extract_reason_contract unit tests ---

def test_extract_reason_contract_from_trigger_candidate():
    from agents.proactive_gate import _extract_reason_contract
    from agents.engagement.triggers import TriggerCandidate
    from datetime import datetime, UTC

    cand = TriggerCandidate(
        source="gmail_unread_threshold",
        pattern="notify",
        payload={"thread_id": "abc123"},
        dedup_key="gmail:abc123",
        decay_at=datetime.now(UTC),
        confidence=0.75,
    )
    rc = _extract_reason_contract(cand)
    assert rc["anchor"] == "abc123"         # falls back to payload["thread_id"]
    assert rc["confidence"] == 0.75
    assert rc["why_now"] is not None        # synthesised from source+pool
    # controls_json must be valid JSON containing the source name
    controls = json.loads(rc["controls_json"])
    assert "snooze_hours" in controls
    # data_checked_json must be valid JSON
    data = json.loads(rc["data_checked_json"])
    assert "gmail" in data                  # inferred from source name


def test_extract_reason_contract_explicit_fields():
    from agents.proactive_gate import _extract_reason_contract

    cand = MagicMock()
    cand.source = "calendar_event_prep"
    cand.pool = "user_anchored"
    cand.confidence = 0.9
    cand.anchor = "event-xyz"
    cand.why_now = "meeting starts in 15 min"
    cand.suggested_action = "review agenda"
    cand.controls = {"snooze_hours": [1], "mute_source": "calendar_event_prep"}
    cand.data_checked = ["calendar"]
    cand.payload = {}

    rc = _extract_reason_contract(cand)
    assert rc["anchor"] == "event-xyz"
    assert rc["why_now"] == "meeting starts in 15 min"
    assert rc["suggested_action"] == "review agenda"
    assert rc["confidence"] == 0.9
    assert json.loads(rc["controls_json"]) == {"snooze_hours": [1], "mute_source": "calendar_event_prep"}
    assert json.loads(rc["data_checked_json"]) == ["calendar"]


def test_extract_reason_contract_none():
    from agents.proactive_gate import _extract_reason_contract
    rc = _extract_reason_contract(None)
    assert rc == {}


def test_extract_reason_contract_dict():
    from agents.proactive_gate import _extract_reason_contract
    cand = {
        "source": "decision_resolve_due",
        "confidence": 0.6,
        "anchor": "dec-7",
        "why_now": "resolve_by passed",
    }
    rc = _extract_reason_contract(cand)
    assert rc["anchor"] == "dec-7"
    assert rc["confidence"] == 0.6
    assert rc["why_now"] == "resolve_by passed"


# --- reserve_and_send integration tests ---

@pytest.mark.asyncio
async def test_reason_contract_written_on_success(isolated_db, monkeypatch):
    """On a successful send, proactive_events row has reason-contract columns set."""
    from agents import proactive_gate
    importlib.reload(proactive_gate)
    monkeypatch.setattr(proactive_gate, "_is_quiet_now", lambda _db=None: False)

    async def ok_send(text):
        return (text, 99, True)

    cand = MagicMock()
    cand.source = "gmail_unread_threshold"
    cand.pool = "user_anchored"
    cand.confidence = 0.8
    cand.anchor = "thread-42"
    cand.why_now = "3 new emails"
    cand.suggested_action = "check inbox"
    cand.controls = {"snooze_hours": [1, 4]}
    cand.data_checked = ["gmail"]
    cand.payload = {}

    result = await proactive_gate.reserve_and_send(
        send_text_fn=ok_send,
        producer_id="gmail_unread_threshold",
        pattern="notify",
        text="you have 3 unread emails",
        candidate=cand,
        db=isolated_db,
    )
    assert result.status == "sent"

    with isolated_db._conn() as c:
        row = dict(c.execute(
            "SELECT anchor, why_now, suggested_action, confidence, "
            "controls_json, data_checked_json FROM proactive_events WHERE id = ?",
            (result.event_id,)
        ).fetchone())

    assert row["anchor"] == "thread-42"
    assert row["why_now"] == "3 new emails"
    assert row["suggested_action"] == "check inbox"
    assert abs(row["confidence"] - 0.8) < 1e-6
    assert json.loads(row["controls_json"]) == {"snooze_hours": [1, 4]}
    assert json.loads(row["data_checked_json"]) == ["gmail"]


@pytest.mark.asyncio
async def test_reason_contract_none_candidate_ok(isolated_db, monkeypatch):
    """reserve_and_send without candidate= still succeeds; reason columns are NULL."""
    from agents import proactive_gate
    importlib.reload(proactive_gate)
    monkeypatch.setattr(proactive_gate, "_is_quiet_now", lambda _db=None: False)

    async def ok_send(text):
        return (text, 100, True)

    result = await proactive_gate.reserve_and_send(
        send_text_fn=ok_send,
        producer_id="heartbeat",
        pattern="notify",
        text="hey",
        db=isolated_db,
    )
    assert result.status == "sent"

    with isolated_db._conn() as c:
        row = dict(c.execute(
            "SELECT anchor, why_now, suggested_action FROM proactive_events WHERE id = ?",
            (result.event_id,)
        ).fetchone())
    # all NULL — no candidate provided
    assert row["anchor"] is None
    assert row["why_now"] is None
    assert row["suggested_action"] is None
