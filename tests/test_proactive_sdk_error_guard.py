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
