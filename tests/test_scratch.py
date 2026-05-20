"""Phase 11: shared session scratch memory."""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from storage import db
from agents import config


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    config.reload()
    yield


def test_scratch_put_and_get_roundtrip():
    db.scratch_put("session-a", "Meria", {"role": "girlfriend", "since": "2024"})
    entries = db.scratch_get("session-a", "Meria")
    assert len(entries) == 1
    assert entries[0]["payload"]["role"] == "girlfriend"


def test_scratch_isolated_per_session():
    db.scratch_put("session-a", "X", "from a")
    db.scratch_put("session-b", "X", "from b")
    assert db.scratch_get("session-a", "X")[0]["payload"] == "from a"
    assert db.scratch_get("session-b", "X")[0]["payload"] == "from b"


def test_scratch_cap_per_session():
    """Insert 105 entries; only 100 most recent retained."""
    for i in range(105):
        db.scratch_put("s", f"topic{i}", f"payload{i}")
    with db._conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM session_scratch WHERE session_id='s'"
        ).fetchone()[0]
    assert n == 100


def test_scratch_cleanup_old_removes_stale():
    """Insert a row with a backdated created_at; cleanup should remove it."""
    db.scratch_put("s", "X", "payload")
    with db._conn() as conn:
        conn.execute(
            "UPDATE session_scratch SET created_at = datetime('now', '-48 hours')"
        )
    removed = db.scratch_cleanup_old(hours=24)
    assert removed == 1


@pytest.mark.asyncio
async def test_scratch_tool_put_and_get():
    from tools import scratch
    # Stub session id
    db.runtime_set("current_session_id", "session-test")
    # Reload scratch module so it picks up the monkeypatched db._DB_PATH
    importlib.reload(scratch)
    out_put = await scratch.scratch_put.handler({"topic": "Meria", "payload": {"k": "v"}})
    assert "saved" in out_put["content"][0]["text"]
    out_get = await scratch.scratch_get.handler({"topic": "Meria", "limit": 5})
    assert len(out_get["data"]["entries"]) == 1
