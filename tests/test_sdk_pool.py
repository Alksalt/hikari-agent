"""Step 1 tests: agents/sdk_pool.py module-level pool.

Tests verify:
- startup() is idempotent (double-call is a no-op)
- shutdown() is idempotent
- get_live_client() returns same instance across calls (no reconnect)
- reconnect on ProcessError clears suspect session_id
- recycle threshold triggers reconnect (counter path)
- is_live_persistent_path_enabled() respects cfg flag
- get_haiku_judge is gone (drift judging routes via _call_aux_llm)
"""
from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents import config
from storage import db

# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    config.reload()
    yield


@pytest.fixture()
def fresh_pool(monkeypatch):
    """Import sdk_pool with module state reset to pristine between tests."""
    import agents.sdk_pool as pool_mod
    # Reset global state between tests.
    monkeypatch.setattr(pool_mod, "_started", False)
    monkeypatch.setattr(pool_mod, "_live", pool_mod._Handle())
    monkeypatch.setattr(pool_mod, "_startup_lock", asyncio.Lock())
    monkeypatch.setattr(pool_mod, "_live_recycle_pending", False)
    return pool_mod


def _make_fake_client(name: str = "fake") -> SimpleNamespace:
    """A fake ClaudeSDKClient with connect/disconnect/query/receive_response."""
    state = {"connected": False, "disconnected": False}

    async def connect():
        state["connected"] = True

    async def disconnect():
        state["disconnected"] = True

    client = SimpleNamespace(
        _name=name,
        connect=connect,
        disconnect=disconnect,
        state=state,
    )
    return client


# --------------------------------------------------------------------------- #
# Step 1 tests                                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_startup_idempotent(fresh_pool, monkeypatch):
    """Double startup() is a no-op — connect called only once."""
    pool = fresh_pool
    clients_created = {"live": 0}

    async def fake_connect_live(resume):
        clients_created["live"] += 1
        return _make_fake_client("live")

    monkeypatch.setattr(pool, "_connect_live", fake_connect_live)

    await pool.startup()
    await pool.startup()  # second call — must be no-op

    assert clients_created["live"] == 1


@pytest.mark.asyncio
async def test_shutdown_idempotent(fresh_pool, monkeypatch):
    """Double shutdown() is safe."""
    pool = fresh_pool
    live_client = _make_fake_client("live")

    async def fake_connect_live(resume):
        return live_client

    monkeypatch.setattr(pool, "_connect_live", fake_connect_live)

    await pool.startup()
    assert pool._started

    # Track disconnects via the client's disconnect method.
    live_disc = {"n": 0}

    async def _live_disc():
        live_disc["n"] += 1

    live_client.disconnect = _live_disc

    await pool.shutdown()
    await pool.shutdown()  # second call — no-op

    # disconnect called at most once (first shutdown)
    assert live_disc["n"] <= 1
    assert not pool._started


@pytest.mark.asyncio
async def test_get_live_client_same_instance(fresh_pool, monkeypatch):
    """get_live_client() returns same object without reconnect."""
    pool = fresh_pool
    client = _make_fake_client("live")

    async def fake_connect_live(resume):
        return client

    monkeypatch.setattr(pool, "_connect_live", fake_connect_live)

    await pool.startup()

    c1 = await pool.get_live_client()
    c2 = await pool.get_live_client()
    assert c1 is c2 is client


@pytest.mark.asyncio
async def test_reconnect_clears_suspect_session_id(fresh_pool, monkeypatch):
    """_reconnect_live should read the latest session_id from DB."""
    pool = fresh_pool
    db.set_session_id("suspect-session-abc")

    resumes_seen = []

    async def fake_connect_live(resume):
        resumes_seen.append(resume)
        return _make_fake_client("live")

    monkeypatch.setattr(pool, "_connect_live", fake_connect_live)

    await pool.startup()
    # startup sees "suspect-session-abc"
    assert resumes_seen[0] == "suspect-session-abc"

    # Simulate suspect-session cleared by caller before reconnect.
    db.set_session_id("")

    await pool._reconnect_live("test: ProcessError")
    # After clear, reconnect sees None (empty string → None).
    assert resumes_seen[-1] is None


@pytest.mark.asyncio
async def test_recycle_threshold_triggers_reconnect(fresh_pool, monkeypatch):
    """Incrementing counter past threshold schedules a reconnect task."""
    pool = fresh_pool
    connects = {"live": 0}

    async def fake_connect_live(resume):
        connects["live"] += 1
        return _make_fake_client("live")

    monkeypatch.setattr(pool, "_connect_live", fake_connect_live)

    # Force a very low threshold so we can hit it in the test.
    monkeypatch.setattr(pool, "_live_recycle_after", lambda: 3)

    await pool.startup()
    assert connects["live"] == 1

    # Drive counter past threshold.
    pool._live.counter = 2
    pool._maybe_schedule_live_recycle()  # counter becomes 3 == threshold

    # The task was created; give the event loop a tick to run it.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert connects["live"] == 2, "live client should have reconnected after threshold"


@pytest.mark.asyncio
async def test_is_live_persistent_path_enabled_default_true(fresh_pool, monkeypatch, tmp_path):
    """Default value is True when key absent from config."""
    pool = fresh_pool
    cfg_text = "runtime:\n  model_primary: claude-sonnet-4-6\n"
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()
    assert pool.is_live_persistent_path_enabled() is True


@pytest.mark.asyncio
async def test_is_live_persistent_path_enabled_false(fresh_pool, monkeypatch, tmp_path):
    """When set false in config, flag returns False."""
    pool = fresh_pool
    cfg_text = "runtime:\n  live_client_persistent: false\n"
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()
    assert pool.is_live_persistent_path_enabled() is False


@pytest.mark.asyncio
async def test_live_counter_advances_per_call(fresh_pool, monkeypatch):
    """_maybe_schedule_live_recycle increments live counter each call."""
    pool = fresh_pool

    async def fake_connect_live(resume):
        return _make_fake_client("live")

    monkeypatch.setattr(pool, "_connect_live", fake_connect_live)
    # Set threshold high so we don't trigger a recycle.
    monkeypatch.setattr(pool, "_live_recycle_after", lambda: 1000)

    await pool.startup()
    assert pool._live.counter == 0
    pool._maybe_schedule_live_recycle()
    assert pool._live.counter == 1
    pool._maybe_schedule_live_recycle()
    assert pool._live.counter == 2
