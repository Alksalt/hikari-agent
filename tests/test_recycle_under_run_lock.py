"""sdk_pool._reconnect_live: lock_run kwarg controls _RUN_LOCK acquisition.

- lock_run=True (default): acquires _RUN_LOCK before connect_lock.
- lock_run=False: skips _RUN_LOCK (for callers already holding it).
Prevents deadlock when called from inside _RUN_LOCK (e.g. ProcessError handler).
"""
from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import patch

import pytest

from storage import db

# ---------------------------------------------------------------------------
# DB isolation
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    yield


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lock_run_false_skips_run_lock():
    """lock_run=False: _RUN_LOCK must NOT be acquired; _do_reconnect_live is called."""
    do_reconnect_calls: list[str] = []

    async def _fake_do_reconnect(reason: str) -> None:
        do_reconnect_calls.append(reason)

    import agents.sdk_pool as pool_mod

    with patch.object(pool_mod, "_do_reconnect_live", side_effect=_fake_do_reconnect):
        # Hold _RUN_LOCK ourselves — if _reconnect_live tries to acquire it
        # with lock_run=True this will deadlock (timeout kills the test).
        from agents.runtime import _RUN_LOCK
        async with _RUN_LOCK:
            await pool_mod._reconnect_live("test reason", lock_run=False)

    assert len(do_reconnect_calls) == 1
    assert "test reason" in do_reconnect_calls[0]


@pytest.mark.asyncio
async def test_lock_run_true_acquires_run_lock():
    """lock_run=True (default): _RUN_LOCK is acquired before _do_reconnect_live."""
    lock_was_locked_during: list[bool] = []

    async def _fake_do_reconnect(reason: str) -> None:
        from agents.runtime import _RUN_LOCK
        # If _RUN_LOCK is locked, locked() returns True.
        lock_was_locked_during.append(_RUN_LOCK.locked())

    import agents.sdk_pool as pool_mod

    with patch.object(pool_mod, "_do_reconnect_live", side_effect=_fake_do_reconnect):
        await pool_mod._reconnect_live("test reason", lock_run=True)

    assert lock_was_locked_during == [True]


@pytest.mark.asyncio
async def test_lock_run_default_is_true():
    """Omitting lock_run behaves the same as lock_run=True."""
    lock_was_locked_during: list[bool] = []

    async def _fake_do_reconnect(reason: str) -> None:
        from agents.runtime import _RUN_LOCK
        lock_was_locked_during.append(_RUN_LOCK.locked())

    import agents.sdk_pool as pool_mod

    with patch.object(pool_mod, "_do_reconnect_live", side_effect=_fake_do_reconnect):
        await pool_mod._reconnect_live("default lock_run test")

    assert lock_was_locked_during == [True]
