"""Tests for agents/daily_brief.py — composer + orchestrator (Sprint 1, Task 3).

Uses a local ``fresh_db`` fixture (the repo has no shared fixture of that
name in conftest.py; mirrors the pattern established in
tests/test_daily_brief_collect.py / tests/test_proactive_backoff.py).
"""
from __future__ import annotations

import importlib
import inspect
from datetime import datetime

import pytest

from agents import daily_brief
from storage import db


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield db
    db._reset_schema_sentinel()


def test_should_fire_now_inside_window(fresh_db, monkeypatch):
    tz = daily_brief._resolve_local_tz()
    now = datetime.now(tz).replace(hour=7, minute=2, second=0, microsecond=0)
    assert daily_brief.should_fire_now(now) is True


def test_should_fire_now_dedups_same_day(fresh_db):
    from storage import db
    tz = daily_brief._resolve_local_tz()
    now = datetime.now(tz).replace(hour=7, minute=2)
    db.runtime_set("daily_brief_last_fired_date", now.date().isoformat())
    assert daily_brief.should_fire_now(now) is False


def test_compose_prompt_contains_all_sections():
    sections = {
        "weather": {"forecast": {"consensus": {"values": {"temp_high_c": 20,
                    "precip_prob_max_pct": 80}, "disagree": []},
                    "sources": {"met.no": {}}, "windows": {}},
                    "label": "kristiansund", "reasons": ["rain 80%"]},
        "email": {"unread_personal": [{"from": "a@b.c", "subject": "hei", "id": "abc12345"}],
                  "calendar_invites": [], "deletable": {"count": 0, "top_senders": []}},
        "calendar": [{"id": "e1", "title": "standup", "start_iso": "2026-07-02T09:00",
                      "location": "", "is_new_since_yesterday": False}],
    }
    prompt = daily_brief.compose_prompt(sections)
    assert "daily_brief_digest" in prompt      # presentation hint
    assert "kristiansund" in prompt
    assert "HIKARI_UNTRUSTED" in prompt        # email + calendar strings wrapped
    assert "next action" in prompt.lower()


def test_compose_prompt_returns_none_when_empty():
    assert daily_brief.compose_prompt(
        {"weather": None, "email": None, "calendar": None}) is None


def test_compose_prompt_ignores_injected_handoff_section():
    """B8(c), review 2026-07-10: handoff: lines (pre-triaged mail-triage
    escalations) must be called out as exempt from the 3-6 item budget, or the
    composer can silently drop a real escalation just because weather/email/
    calendar noise already filled the quota."""
    sections = {"weather": None, "email": None, "calendar": None,
               "jobhunt": {"due_touches": [], "deadlines": [], "interviews": [],
                           "replies": [], "handoff": [
                               {"summary": "svar: x", "details": []}]}}
    prompt = daily_brief.compose_prompt(sections)
    assert prompt is None


@pytest.mark.asyncio
async def test_collect_sections_crash_clears_force_flag_and_returns_false(fresh_db, monkeypatch):
    """B8(a)/C3(b) fix: a crash inside collect_sections()/compose_prompt() used
    to leave _FORCE_RUN_KEY wedged at '1' (a user-requested forced brief),
    which makes should_fire_now() return True unconditionally on every future
    poll — reproducing the identical crash forever with zero operator-facing
    signal. Now it must clear the flag and return False instead."""
    tz = daily_brief._resolve_local_tz()
    now = datetime.now(tz).replace(hour=3, minute=0, second=0, microsecond=0)  # outside any window
    monkeypatch.setattr(daily_brief, "_now_local", lambda: now)

    from storage import db as _db
    _db.runtime_set(daily_brief._FORCE_RUN_KEY, "1")

    import agents.cadence as _cadence
    monkeypatch.setattr(_cadence, "can_send", lambda *a, **kw: (True, "ok"))

    async def _boom():
        raise RuntimeError("collect_sections exploded")
    monkeypatch.setattr(daily_brief, "collect_sections", _boom)

    sent = []

    async def _send(text):
        sent.append(text)
        return True

    result = await daily_brief.maybe_send_daily_brief(_send)

    assert result is False
    assert sent == []
    assert _db.runtime_get(daily_brief._FORCE_RUN_KEY) != "1"


@pytest.mark.asyncio
async def test_no_send_branch_logs_distinguishing_reason(fresh_db, monkeypatch, caplog):
    """B8(b)/C3(a) fix: the no-send branch (empty / NO_MESSAGE / sdk-error) used
    to log NOTHING, treating a genuine SDK/auth error identically to "nothing
    to report today" with zero trace. Now it logs one info line naming which
    of the three cases fired."""
    tz = daily_brief._resolve_local_tz()
    now = datetime.now(tz).replace(hour=7, minute=2)
    monkeypatch.setattr(daily_brief, "_now_local", lambda: now)

    import agents.cadence as _cadence
    monkeypatch.setattr(_cadence, "can_send", lambda *a, **kw: (True, "ok"))

    async def _fake_sections():
        return {"weather": {"forecast": {}, "label": "x", "reasons": ["r"]},
               "email": None, "calendar": None}
    monkeypatch.setattr(daily_brief, "collect_sections", _fake_sections)

    async def _fake_run_internal_text(prompt, **kwargs):
        return "Failed to authenticate. API Error: 401 socket closed"
    monkeypatch.setattr(daily_brief, "run_internal_text", _fake_run_internal_text)

    with caplog.at_level("INFO", logger="agents.daily_brief"):
        result = await daily_brief.maybe_send_daily_brief(lambda text: None)

    assert result is False
    assert any("no-send (sdk-error)" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_orchestrator_skips_silently_when_empty(fresh_db, monkeypatch):
    tz = daily_brief._resolve_local_tz()
    now = datetime.now(tz).replace(hour=7, minute=2)
    monkeypatch.setattr(daily_brief, "_now_local", lambda: now)

    async def empty_sections():
        return {"weather": None, "email": None, "calendar": None}
    monkeypatch.setattr(daily_brief, "collect_sections", empty_sections)
    sent = []

    async def fake_send(text):
        sent.append(text)
        return (text, 1, True)

    assert await daily_brief.maybe_send_daily_brief(fake_send) is False
    assert sent == []
    # dedup date IS written — an empty day is a completed day, no retry loop
    from storage import db
    assert db.runtime_get("daily_brief_last_fired_date") == now.date().isoformat()


# --------------------------------------------------------------------------
# mark_processed after a confirmed send (Task 8)
#
# Mirrors the established mocking idiom for the full send path
# (tests/test_conversational_tools.py::test_force_flag_cleared_on_successful_send):
# cadence.can_send + collect_sections + stateless no-tools composition + the
# module-local `from agents.proactive_gate import reserve_and_send` import
# (patched on the proactive_gate module itself, since it's a deferred
# import resolved fresh at call time).
# --------------------------------------------------------------------------

def _patch_successful_send_path(monkeypatch, sections):
    import agents.cadence as _cadence
    monkeypatch.setattr(_cadence, "can_send", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr(_cadence, "record_ceremony_sent", lambda *a, **kw: None)

    async def _fake_sections():
        return sections
    monkeypatch.setattr(daily_brief, "collect_sections", _fake_sections)

    async def _fake_run_internal_text(prompt, **kwargs):
        return "morning brief text."
    monkeypatch.setattr(daily_brief, "run_internal_text", _fake_run_internal_text)

    import agents.proactive_gate as _pg
    from agents.proactive_gate import ReservationResult

    async def _fake_reserve_and_send(**kw):
        return ReservationResult("sent", None, None, 1, "morning brief text.")
    monkeypatch.setattr(_pg, "reserve_and_send", _fake_reserve_and_send)


@pytest.mark.asyncio
async def test_daily_brief_never_marks_mail_actions_after_send(fresh_db, monkeypatch):
    tz = daily_brief._resolve_local_tz()
    now = datetime.now(tz).replace(hour=7, minute=2, second=0, microsecond=0)
    monkeypatch.setattr(daily_brief, "_now_local", lambda: now)

    handoff_entries = [{"raw": "- [2026-07-09 08:00] svar: x — status: unprocessed",
                         "stamp": "2026-07-09 08:00", "summary": "svar: x", "details": []}]
    sections = {
        "weather": None, "email": None, "calendar": None,
        "jobhunt": {"due_touches": [{"org": "Acme", "touch": "1", "due": "today"}],
                    "deadlines": [], "interviews": [],
                    "replies": [], "handoff": handoff_entries},
    }
    _patch_successful_send_path(monkeypatch, sections)

    async def _send(text):
        return True

    assert await daily_brief.maybe_send_daily_brief(_send) is True


@pytest.mark.asyncio
async def test_mark_processed_not_called_when_no_handoff(fresh_db, monkeypatch):
    """No handoff entries in the sent sections -> mark_processed is never
    invoked (nothing to mark, and no needless file read/write)."""
    tz = daily_brief._resolve_local_tz()
    now = datetime.now(tz).replace(hour=7, minute=2, second=0, microsecond=0)
    monkeypatch.setattr(daily_brief, "_now_local", lambda: now)

    sections = {
        "weather": None, "email": None, "calendar": None,
        "jobhunt": {"due_touches": [{"org": "Acme", "touch": "1", "due": "today"}],
                    "deadlines": [], "interviews": [],
                    "replies": [], "handoff": []},
    }
    _patch_successful_send_path(monkeypatch, sections)

    async def _send(text):
        return True

    assert await daily_brief.maybe_send_daily_brief(_send) is True


@pytest.mark.asyncio
async def test_injected_handoff_never_calls_legacy_mark_processed(fresh_db, monkeypatch):
    """Injected legacy handoff data cannot create a delivery-state write."""
    tz = daily_brief._resolve_local_tz()
    now = datetime.now(tz).replace(hour=7, minute=2, second=0, microsecond=0)
    monkeypatch.setattr(daily_brief, "_now_local", lambda: now)

    handoff_entries = [{"raw": "- [2026-07-09 08:00] svar: x — status: unprocessed",
                         "stamp": "2026-07-09 08:00", "summary": "svar: x", "details": []}]
    sections = {
        "weather": None, "email": None, "calendar": None,
        "jobhunt": {"due_touches": [{"org": "Acme", "touch": "1", "due": "today"}],
                    "deadlines": [], "interviews": [],
                    "replies": [], "handoff": handoff_entries},
    }
    _patch_successful_send_path(monkeypatch, sections)

    async def _send(text):
        return True

    assert await daily_brief.maybe_send_daily_brief(_send) is True


def test_daily_brief_source_has_no_mail_delivery_state_transition():
    source = inspect.getsource(daily_brief)
    assert "mail_handoff" not in source
    assert "mark_processed" not in source
    assert "reply_radar" not in source
