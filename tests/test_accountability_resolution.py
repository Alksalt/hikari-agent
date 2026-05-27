"""Tests for accountability_resolve tool and stats helper."""
from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agents import config
from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    config.reload()
    yield


def _make_item() -> tuple[int, int, int]:
    fire_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    follow_at = (datetime.now(UTC) + timedelta(hours=4)).isoformat()
    return db.accountability_create_atomic(fire_at, follow_at, "test task")


@pytest.mark.asyncio
async def test_resolve_did():
    from tools.reminders.accountability import accountability_resolve
    rid, follow_rid, item_id = _make_item()
    out = await accountability_resolve.handler({"id": item_id, "outcome": 1})
    assert "logged" in out["content"][0]["text"].lower()
    item = db.accountability_get(item_id)
    assert item["outcome"] == 1
    assert item["resolved_at"] is not None


@pytest.mark.asyncio
async def test_resolve_didnt():
    from tools.reminders.accountability import accountability_resolve
    rid, follow_rid, item_id = _make_item()
    out = await accountability_resolve.handler({"id": item_id, "outcome": 0})
    item = db.accountability_get(item_id)
    assert item["outcome"] == 0
    assert item["resolved_at"] is not None


@pytest.mark.asyncio
async def test_resolve_clears_pending_key(monkeypatch):
    from tools.reminders.accountability import accountability_resolve
    rid, follow_rid, item_id = _make_item()
    db.runtime_set("pending_accountability_check", str(item_id))
    await accountability_resolve.handler({"id": item_id, "outcome": 1})
    assert db.runtime_get("pending_accountability_check") is None


@pytest.mark.asyncio
async def test_resolve_other_item_does_not_clear_pending():
    from tools.reminders.accountability import accountability_resolve
    rid, follow_rid, item_id_a = _make_item()
    rid2, follow_rid2, item_id_b = _make_item()
    db.runtime_set("pending_accountability_check", str(item_id_a))
    await accountability_resolve.handler({"id": item_id_b, "outcome": 1})
    assert db.runtime_get("pending_accountability_check") == str(item_id_a)


@pytest.mark.asyncio
async def test_invalid_outcome_refused():
    from tools.reminders.accountability import accountability_resolve
    rid, follow_rid, item_id = _make_item()
    out = await accountability_resolve.handler({"id": item_id, "outcome": 2})
    assert "refused" in out["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_missing_item_refused():
    from tools.reminders.accountability import accountability_resolve
    out = await accountability_resolve.handler({"id": 9999, "outcome": 1})
    assert "refused" in out["content"][0]["text"].lower()


def test_stats_three_resolved_one_unresolved():
    # 2 did (outcome=1), 1 didn't (outcome=0), 1 unresolved
    for _ in range(2):
        _, _, item_id = _make_item()
        db.accountability_resolve(item_id, 1)

    _, _, item_id2 = _make_item()
    db.accountability_resolve(item_id2, 0)

    _make_item()  # unresolved

    stats = db.accountability_stats()
    assert stats["total"] == 4
    assert stats["resolved"] == 3
    assert stats["did"] == 2
    assert stats["didnt"] == 1
    assert abs(stats["did_rate"] - (2 / 3)) < 0.001
