"""FIX 3: session_handoff is peeked (non-destructive) at block-build time and
only cleared after it survives the texture budget. Consuming eagerly at build
time destroyed a budget-dropped handoff without ever injecting it.
"""
from __future__ import annotations

import asyncio
import importlib
import json
from datetime import UTC, datetime, timedelta

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from agents import config
    config.reload()
    yield
    config.reload()


def _seed_handoff():
    from storage import db
    # 2h ago: inside the [min_gap=0.5h, max_gap=48h] injection window.
    ts = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    db.runtime_set("session_handoff", json.dumps({
        "ts": ts,
        "turns": [{"role": "user", "content": "where were we on the migration"}],
    }))


def test_format_session_handoff_peek_is_non_destructive():
    from agents import hooks
    _seed_handoff()
    first = hooks._format_session_handoff()
    assert "session handoff" in first
    # Peek must NOT consume — a second build still sees it. Before FIX 3 the
    # first call consumed it and the second returned "".
    second = hooks._format_session_handoff()
    assert second == first


def test_inject_memory_consumes_handoff_only_after_selection():
    from agents.hooks import inject_memory
    from storage import db
    _seed_handoff()
    out = asyncio.run(inject_memory({"prompt": "hey"}, None, None))
    ctx = out.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "session handoff" in ctx
    # Selected + injected → now consumed so it is not echoed twice.
    assert db.runtime_get("session_handoff") in (None, "")
