"""Tests for agents/mail_decisions.py (Sprint 2, Task 6) — the ask-user
question loop: urgent proactive push, non-urgent silence (left for the
brief), asked-once tracking via mark-surfaced, and fetch_current_row()
lookup for the mail_action_decide chat tool.

Mirrors the isolated-db + gate-open fixture pattern from
tests/test_decision_log.py, and the owner-CLI monkeypatch pattern from
tests/test_mail_handoff.py.
"""
from __future__ import annotations

import importlib
from unittest.mock import AsyncMock

import pytest

from agents import mail_decisions


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    yield


@pytest.fixture(autouse=True)
def _gate_open(monkeypatch):
    """Proactive gate never suppresses due to quiet hours/silence in these
    unit tests — those paths are covered elsewhere."""
    import agents.proactive_gate as _gate
    monkeypatch.setattr(_gate, "_is_quiet_now", lambda _db=None: False)
    monkeypatch.setattr(_gate, "_silence_active", lambda _db: False)


def _ask_user_row(action_id=17, priority=0, surface_count=0, decision=None, **overrides):
    row = {
        "id": action_id,
        "kind": "ask-user",
        "priority": priority,
        "headline": "Feil adresse — send søknad til ny kontakt?",
        "details": [],
        "options": [
            {"id": "opt-a", "label": "ja, send til ny adresse"},
            {"id": "opt-b", "label": "nei, behold gammel"},
        ],
        "decision": decision,
        "surface_count": surface_count,
        "created_at": "2026-07-12T08:00:00Z",
    }
    row.update(overrides)
    return row


# --------------------------------------------------------------------------
# unasked_ask_user_rows / _list_payload filtering
# --------------------------------------------------------------------------

def test_unasked_ask_user_rows_filters_kind_decision_and_surface_count(monkeypatch):
    rows = [
        _ask_user_row(action_id=1),                              # eligible
        _ask_user_row(action_id=2, surface_count=1),              # already asked
        _ask_user_row(action_id=3, decision="opt-a"),             # already decided
        {"id": 4, "kind": "interview_invite", "priority": 0,
         "headline": "x", "options": [], "decision": None, "surface_count": 0},
    ]
    out = mail_decisions.unasked_ask_user_rows(rows)
    assert [r["id"] for r in out] == [1]


def test_list_payload_returns_none_when_cli_unavailable(monkeypatch):
    monkeypatch.setattr(mail_decisions.mail_handoff, "_run_cli", lambda *a, **kw: None)
    assert mail_decisions._list_payload() is None
    assert mail_decisions.unasked_ask_user_rows() == []


# --------------------------------------------------------------------------
# poll_and_ask — urgent proactive push
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_urgent_ask_user_row_sends_exactly_once_with_options_and_action_id(monkeypatch):
    row = _ask_user_row(action_id=17, priority=0)
    monkeypatch.setattr(mail_decisions, "_list_payload", lambda **kw: [row])

    marked = []
    monkeypatch.setattr(
        mail_decisions.mail_handoff, "mark_surfaced",
        lambda entries: marked.append(entries) or True,
    )

    send = AsyncMock(return_value=("ok", 1, True))
    n = await mail_decisions.poll_and_ask(send)

    assert n == 1
    send.assert_awaited_once()
    text = send.call_args.args[0]
    assert "ja, send til ny adresse" in text
    assert "nei, behold gammel" in text
    assert "[action #17]" in text
    assert marked == [[{"action_id": 17}]]


@pytest.mark.asyncio
async def test_non_urgent_ask_user_row_never_sent_proactively(monkeypatch):
    row = _ask_user_row(action_id=18, priority=1)  # important, not urgent
    monkeypatch.setattr(mail_decisions, "_list_payload", lambda **kw: [row])
    marked = []
    monkeypatch.setattr(
        mail_decisions.mail_handoff, "mark_surfaced",
        lambda entries: marked.append(entries) or True,
    )

    send = AsyncMock(return_value=("ok", 1, True))
    n = await mail_decisions.poll_and_ask(send)

    assert n == 0
    send.assert_not_awaited()
    assert marked == []  # non-urgent rows are left untouched for the brief


@pytest.mark.asyncio
async def test_already_asked_urgent_row_is_not_resent(monkeypatch):
    row = _ask_user_row(action_id=19, priority=0, surface_count=1)
    monkeypatch.setattr(mail_decisions, "_list_payload", lambda **kw: [row])

    send = AsyncMock(return_value=("ok", 1, True))
    n = await mail_decisions.poll_and_ask(send)

    assert n == 0
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_already_decided_urgent_row_is_not_resent(monkeypatch):
    row = _ask_user_row(action_id=20, priority=0, decision="opt-a")
    monkeypatch.setattr(mail_decisions, "_list_payload", lambda **kw: [row])

    send = AsyncMock(return_value=("ok", 1, True))
    n = await mail_decisions.poll_and_ask(send)

    assert n == 0
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_and_ask_does_nothing_when_cli_unavailable(monkeypatch):
    monkeypatch.setattr(mail_decisions, "_list_payload", lambda **kw: None)
    send = AsyncMock(return_value=("ok", 1, True))
    n = await mail_decisions.poll_and_ask(send)
    assert n == 0
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_and_ask_disabled_by_jobhunt_config(monkeypatch):
    from agents import config as cfg
    original_get = cfg.get
    monkeypatch.setattr(
        cfg, "get",
        lambda k, d=None: False if k == "jobhunt.enabled" else original_get(k, d),
    )
    monkeypatch.setattr(
        mail_decisions, "_list_payload",
        lambda **kw: (_ for _ in ()).throw(AssertionError("must not fetch when disabled")),
    )
    send = AsyncMock(return_value=("ok", 1, True))
    n = await mail_decisions.poll_and_ask(send)
    assert n == 0
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_and_ask_disabled_by_own_config(monkeypatch):
    from agents import config as cfg
    original_get = cfg.get
    monkeypatch.setattr(
        cfg, "get",
        lambda k, d=None: False if k == "mail_decisions.enabled" else original_get(k, d),
    )
    send = AsyncMock(return_value=("ok", 1, True))
    n = await mail_decisions.poll_and_ask(send)
    assert n == 0
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_and_ask_survives_mark_surfaced_exception(monkeypatch):
    """A crash marking surfaced must not un-send or crash the poll — the
    question was genuinely delivered; only the repeat-guard bookkeeping
    failed (logged, not fatal)."""
    row = _ask_user_row(action_id=21, priority=0)
    monkeypatch.setattr(mail_decisions, "_list_payload", lambda **kw: [row])
    monkeypatch.setattr(
        mail_decisions.mail_handoff, "mark_surfaced",
        lambda entries: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    send = AsyncMock(return_value=("ok", 1, True))
    n = await mail_decisions.poll_and_ask(send)
    assert n == 1  # still counted as sent


@pytest.mark.asyncio
async def test_multiple_urgent_rows_each_get_one_send(monkeypatch):
    rows = [_ask_user_row(action_id=1, priority=0), _ask_user_row(action_id=2, priority=0)]
    monkeypatch.setattr(mail_decisions, "_list_payload", lambda **kw: rows)
    monkeypatch.setattr(mail_decisions.mail_handoff, "mark_surfaced", lambda entries: True)

    send = AsyncMock(return_value=("ok", 1, True))
    n = await mail_decisions.poll_and_ask(send)
    assert n == 2
    assert send.await_count == 2


# --------------------------------------------------------------------------
# fetch_current_row — always re-fetches, never trusts stale/model memory
# --------------------------------------------------------------------------

def test_fetch_current_row_returns_matching_row(monkeypatch):
    rows = [_ask_user_row(action_id=5), _ask_user_row(action_id=6)]
    monkeypatch.setattr(mail_decisions, "_list_payload", lambda **kw: rows)
    row = mail_decisions.fetch_current_row(6)
    assert row["id"] == 6


def test_fetch_current_row_none_when_not_pending(monkeypatch):
    monkeypatch.setattr(mail_decisions, "_list_payload", lambda **kw: [_ask_user_row(action_id=5)])
    assert mail_decisions.fetch_current_row(999) is None


def test_fetch_current_row_none_when_cli_unavailable(monkeypatch):
    monkeypatch.setattr(mail_decisions, "_list_payload", lambda **kw: None)
    assert mail_decisions.fetch_current_row(5) is None


def test_fetch_current_row_uses_generous_decide_lookup_cap(monkeypatch):
    seen = {}

    def fake_list_payload(low_priority_cap=None):
        seen["cap"] = low_priority_cap
        return [_ask_user_row(action_id=5)]

    monkeypatch.setattr(mail_decisions, "_list_payload", fake_list_payload)
    mail_decisions.fetch_current_row(5)
    assert seen["cap"] == 1000  # config default mail_actions.decide_lookup_cap


# --------------------------------------------------------------------------
# mail_action_decide chat tool — maps option_number from the FRESH payload
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_decide_tool_maps_option_number_to_fresh_option_id(monkeypatch):
    from tools.mail_actions import decide as decide_tool

    row = _ask_user_row(action_id=17)
    monkeypatch.setattr(decide_tool.mail_decisions, "fetch_current_row", lambda aid: row)

    calls = []

    def fake_decide(action_id, option_id, note=""):
        calls.append((action_id, option_id))
        return True, {"id": action_id, "decision": option_id}

    monkeypatch.setattr(decide_tool.mail_handoff, "decide", fake_decide)

    result = await decide_tool.mail_action_decide.handler({"action_id": 17, "option_number": 2})

    assert calls == [(17, "opt-b")]  # 1-based option_number=2 -> second option's id
    assert "registrerte" in str(result).lower()


@pytest.mark.asyncio
async def test_decide_tool_out_of_range_option_number_calls_nothing(monkeypatch):
    from tools.mail_actions import decide as decide_tool

    row = _ask_user_row(action_id=17)  # only 2 options
    monkeypatch.setattr(decide_tool.mail_decisions, "fetch_current_row", lambda aid: row)

    def fake_decide(*a, **kw):
        raise AssertionError("mail_handoff.decide must not be called for an out-of-range option")

    monkeypatch.setattr(decide_tool.mail_handoff, "decide", fake_decide)

    result = await decide_tool.mail_action_decide.handler({"action_id": 17, "option_number": 5})
    assert "ugyldig" in str(result).lower()


@pytest.mark.asyncio
async def test_decide_tool_unknown_action_id_calls_nothing(monkeypatch):
    from tools.mail_actions import decide as decide_tool

    monkeypatch.setattr(decide_tool.mail_decisions, "fetch_current_row", lambda aid: None)

    def fake_decide(*a, **kw):
        raise AssertionError("mail_handoff.decide must not be called for an unknown action id")

    monkeypatch.setattr(decide_tool.mail_handoff, "decide", fake_decide)

    result = await decide_tool.mail_action_decide.handler({"action_id": 999, "option_number": 1})
    assert "fant ikke" in str(result).lower()


@pytest.mark.asyncio
async def test_decide_tool_rejects_non_ask_user_row(monkeypatch):
    from tools.mail_actions import decide as decide_tool

    row = {"id": 5, "kind": "interview_invite", "options": []}
    monkeypatch.setattr(decide_tool.mail_decisions, "fetch_current_row", lambda aid: row)

    def fake_decide(*a, **kw):
        raise AssertionError("mail_handoff.decide must not be called for a non-ask-user row")

    monkeypatch.setattr(decide_tool.mail_handoff, "decide", fake_decide)

    result = await decide_tool.mail_action_decide.handler({"action_id": 5, "option_number": 1})
    assert "ikke et ask-user" in str(result).lower()


@pytest.mark.asyncio
async def test_decide_tool_surfaces_rejection_message_from_owner_cli(monkeypatch):
    """mail_handoff.decide returning (False, message) on a rejected
    transition must be surfaced to the user, not swallowed/retried."""
    from tools.mail_actions import decide as decide_tool

    row = _ask_user_row(action_id=17)
    monkeypatch.setattr(decide_tool.mail_decisions, "fetch_current_row", lambda aid: row)
    monkeypatch.setattr(
        decide_tool.mail_handoff, "decide",
        lambda aid, oid, note="": (False, "Handling 17 er allerede løst"),
    )

    result = await decide_tool.mail_action_decide.handler({"action_id": 17, "option_number": 1})
    assert "allerede løst" in str(result)
