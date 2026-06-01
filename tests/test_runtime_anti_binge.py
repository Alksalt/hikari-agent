"""Phase B — anti-binge gate: session turn limit + session rotation reset."""
from __future__ import annotations

from pathlib import Path

import pytest

from agents import config as cfg
from agents import runtime
from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    cfg.reload()
    yield
    db._reset_schema_sentinel()


@pytest.mark.asyncio
async def test_anti_binge_short_circuits_after_limit(monkeypatch):
    monkeypatch.setattr(
        cfg, "get",
        lambda k, d=None: 5 if k == "working_memory.anti_binge_turn_limit" else d,
    )
    db.set_session_id("test-sess-1")
    called = {"n": 0}

    async def fake_invoke(*a, **kw):
        called["n"] += 1
        return "ok"

    monkeypatch.setattr(runtime, "_invoke_sdk", fake_invoke)
    for _ in range(5):
        out = await runtime.run_user_turn("hi")
        assert out == "ok"
    out = await runtime.run_user_turn("hi")
    assert out in runtime._MOSHFEGH_LINES
    assert called["n"] == 5  # SDK NOT called on the close turn


@pytest.mark.asyncio
async def test_session_rotation_resets_counter(monkeypatch):
    monkeypatch.setattr(
        cfg, "get",
        lambda k, d=None: 2 if k == "working_memory.anti_binge_turn_limit" else d,
    )

    async def fake_invoke(*a, **kw):
        return "ok"

    monkeypatch.setattr(runtime, "_invoke_sdk", fake_invoke)
    db.set_session_id("sess-A")
    for _ in range(3):
        await runtime.run_user_turn("hi")
    assert (db.runtime_get("session_closed") or "") == "true"
    db.set_session_id("sess-B")
    out = await runtime.run_user_turn("hi")
    assert out == "ok"
    assert db.runtime_get_int("session_turn_count") == 1
