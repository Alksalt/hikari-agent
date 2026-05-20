"""SDK-error guard in the visible-proactive send paths.

The guard ``looks_like_sdk_error`` is defined in ``agents/runtime.py`` and
must be called by every visible-proactive sender — heartbeat, re-engage,
calendar heartbeat — BEFORE shipping ``text`` to Telegram. Otherwise a
transient auth/network error string can leak as the message body (the
2026-05-20 401 incident).

These tests confirm: when ``run_proactive`` (aliased to
``run_visible_proactive``) returns a string that matches the SDK-error
pattern, none of the three senders call ``send_text``.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agents import config
from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    config.reload()
    yield


_SDK_ERROR_BODY = (
    "Failed to authenticate. API Error: 401 The socket connection was "
    "closed unexpectedly. For more information, pass `verbose: true` "
    "in the second argument to fetch()"
)


@pytest.mark.asyncio
async def test_heartbeat_refuses_to_send_sdk_error_string(monkeypatch):
    """Bug: SDK 401 leak shipped as heartbeat body. Fix: guard at line 239+."""
    from agents import proactive

    # Force the heartbeat path to think a send is allowed.
    monkeypatch.setattr(proactive, "should_send_heartbeat", lambda: True)
    monkeypatch.setattr(proactive, "_pick_seed",
                        lambda: (0, "seed", "morning"))
    monkeypatch.setattr(proactive.cadence, "can_send_proactive",
                        lambda src: (True, src))
    monkeypatch.setattr(proactive, "_mood_from_core", lambda: "focused")
    monkeypatch.setattr(proactive, "_build_prompt", lambda mood, seed: "PROMPT")

    async def _fake_proactive(prompt):
        return _SDK_ERROR_BODY

    monkeypatch.setattr(proactive, "run_proactive", _fake_proactive)

    send_text = AsyncMock()
    result = await proactive.maybe_send_heartbeat(send_text)

    assert result is False, "guard should refuse to send"
    send_text.assert_not_called()


@pytest.mark.asyncio
async def test_reengage_refuses_to_send_sdk_error_string(monkeypatch):
    from agents import proactive

    monkeypatch.setattr(proactive, "should_send_reengagement", lambda: True)
    monkeypatch.setattr(proactive.cadence, "can_send_proactive",
                        lambda src: (True, src))
    monkeypatch.setattr(proactive, "_mood_from_core", lambda: "focused")

    async def _fake_proactive(prompt):
        return _SDK_ERROR_BODY

    monkeypatch.setattr(proactive, "run_proactive", _fake_proactive)

    send_text = AsyncMock()
    result = await proactive.maybe_send_reengagement(send_text)

    assert result is False
    send_text.assert_not_called()


@pytest.mark.asyncio
async def test_calendar_heartbeat_refuses_to_send_sdk_error_string(monkeypatch):
    from agents import proactive
    from agents import config

    # Force calendar_heartbeat enabled + a single eligible event.
    monkeypatch.setattr(config, "get", _stub_cfg_get(config))

    fake_event = {"id": "evt-abc", "title": "1:1 with Alex",
                  "start": "2026-05-20T14:00:00+00:00",
                  "end": "2026-05-20T15:00:00+00:00"}

    monkeypatch.setattr(proactive, "_fetch_upcoming_events",
                        AsyncMock(return_value=[fake_event]))
    monkeypatch.setattr(proactive, "_event_duration_minutes",
                        lambda ev: 60.0)
    monkeypatch.setattr(proactive, "_minutes_until_start",
                        lambda ev: 30.0)
    monkeypatch.setattr(proactive, "_calendar_event_signature",
                        lambda ev: "sig-abc")
    monkeypatch.setattr(proactive, "_calendar_event_already_notified",
                        lambda sig: False)
    monkeypatch.setattr(proactive, "_mark_calendar_event_notified",
                        lambda sig: None)
    monkeypatch.setattr(proactive.cadence, "can_send_proactive",
                        lambda src: (True, src))
    monkeypatch.setattr(proactive, "_mood_from_core", lambda: "focused")
    monkeypatch.setattr(proactive, "_build_prompt", lambda mood, seed: "PROMPT")

    async def _fake_proactive(prompt):
        return _SDK_ERROR_BODY

    monkeypatch.setattr(proactive, "run_proactive", _fake_proactive)

    send_text = AsyncMock()
    result = await proactive.maybe_send_calendar_heartbeat(send_text)

    assert result is False
    send_text.assert_not_called()


def _stub_cfg_get(config_module):
    """Override cfg.get to force the calendar_heartbeat gate open."""
    real = config_module.get

    def _get(key, default=None):
        if key == "calendar_heartbeat.enabled":
            return True
        if key == "calendar_heartbeat.lookahead_minutes":
            return 120
        if key == "calendar_heartbeat.min_event_duration_minutes":
            return 15
        if key == "calendar_heartbeat.exclude_calendar_ids":
            return []
        if key == "calendar_heartbeat.prep_message_lead_minutes":
            return 30
        if key == "calendar_heartbeat.lead_window_jitter_minutes":
            return 60  # wide so our fake event lands in the band
        return real(key, default)

    return _get


def test_research_subagent_can_call_web_tools(monkeypatch):
    """R-F regression: WebFetch / WebSearch must be in the parent allowlist
    so the research subagent can invoke them when spawned via Agent."""
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "0")
    import importlib
    from agents import runtime
    importlib.reload(runtime)

    names = runtime.allowed_tool_names()
    assert "WebFetch" in names, (
        "WebFetch missing from parent allowlist — research subagent silently fails"
    )
    assert "WebSearch" in names, (
        "WebSearch missing from parent allowlist — research subagent silently fails"
    )
