import pytest

from tools.mail_actions import decide, update


@pytest.mark.asyncio
async def test_acknowledge_calls_owner_adapter(monkeypatch):
    calls = []
    monkeypatch.setattr(update.mail_handoff, "acknowledge", lambda action_id: calls.append(action_id) or True)
    result = await update.mail_action_acknowledge.handler({"action_id": 20})
    assert calls == [20]
    assert "acknowledged" in str(result)


@pytest.mark.asyncio
async def test_resolve_calls_owner_adapter_with_note(monkeypatch):
    calls = []
    monkeypatch.setattr(update.mail_handoff, "resolve",
                        lambda action_id, note: calls.append((action_id, note)) or True)
    result = await update.mail_action_resolve.handler({"action_id": 21, "note": "done"})
    assert calls == [(21, "done")]
    assert "resolved" in str(result)


@pytest.mark.asyncio
async def test_snooze_calls_owner_adapter(monkeypatch):
    calls = []
    monkeypatch.setattr(update.mail_handoff, "snooze",
                        lambda action_id, until: calls.append((action_id, until)) or True)
    result = await update.mail_action_snooze.handler(
        {"action_id": 22, "until_iso": "2026-07-20T09:00:00Z"}
    )
    assert calls == [(22, "2026-07-20T09:00:00Z")]
    assert "snoozed" in str(result)


@pytest.mark.asyncio
async def test_decide_calls_owner_adapter_with_option_id_from_fresh_row(monkeypatch):
    """Mirrors the other three tools' pattern; deeper coverage (out-of-range
    option numbers, unknown action ids, non-ask-user rows, rejected
    transitions) lives in tests/test_mail_decisions.py."""
    row = {
        "id": 23, "kind": "ask-user",
        "options": [{"id": "opt-a", "label": "a"}, {"id": "opt-b", "label": "b"}],
    }
    monkeypatch.setattr(decide.mail_decisions, "fetch_current_row", lambda action_id: row)
    calls = []
    monkeypatch.setattr(
        decide.mail_handoff, "decide",
        lambda action_id, option_id, note="": calls.append((action_id, option_id)) or (True, {}),
    )
    result = await decide.mail_action_decide.handler({"action_id": 23, "option_number": 2})
    assert calls == [(23, "opt-b")]
    assert "registrerte" in str(result).lower()
