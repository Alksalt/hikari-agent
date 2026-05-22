"""Tests for the per-thread SQLite connection pool in storage/db.py.

Verifies that 50 concurrent async writes don't produce 'database is locked'
errors — previously the per-call connect/close pattern left windows where
concurrent writers would race, and WAL alone didn't protect against that
under rapid concurrent writes on the same thread.
"""
from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import pytest

from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield


@pytest.mark.asyncio
async def test_50_parallel_writes_no_locked_error():
    """50 concurrent asyncio tasks each writing a message row must not raise.

    Schema is initialised on the main thread first; workers only write rows,
    so no ALTER TABLE races occur. This mirrors real usage where the bridge
    boots and calls db once before spawning background workers.
    """
    # Prime the schema on the current (test) thread before spawning workers.
    db._get_pooled_conn()

    async def write_one(i: int):
        await asyncio.to_thread(db.append_message, "user", f"msg {i}")

    results = await asyncio.gather(
        *[write_one(i) for i in range(50)],
        return_exceptions=True,
    )
    errors = [r for r in results if isinstance(r, Exception)]
    assert errors == [], f"got errors: {errors}"

    with db._conn() as c:
        count = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == 50


@pytest.mark.asyncio
async def test_connection_pool_reuses_connection():
    """The same connection object must be returned on consecutive calls
    within the same thread."""
    conn1 = db._get_pooled_conn()
    conn2 = db._get_pooled_conn()
    assert conn1 is conn2


@pytest.mark.asyncio
async def test_reset_sentinel_triggers_reconnect(tmp_path: Path, monkeypatch):
    """After _reset_schema_sentinel(), the next _get_pooled_conn call opens
    a new connection against the (potentially changed) DB path."""
    conn1 = db._get_pooled_conn()
    db._reset_schema_sentinel()
    # Patch to a new path to force a reconnect.
    new_path = tmp_path / "other.db"
    monkeypatch.setattr(db, "_DB_PATH", new_path)
    conn2 = db._get_pooled_conn()
    assert conn1 is not conn2
