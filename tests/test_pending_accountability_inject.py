"""Tests for _format_pending_accountability TTL anchor logic."""
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


@pytest.mark.asyncio
async def test_pending_accountability_within_ttl_returns_block(monkeypatch):
    """Pending key within TTL → inject block returned."""
    from agents.hooks import _format_pending_accountability

    rid = db.reminder_insert(fire_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(), text="x", lead_minutes=0)
    frid = db.reminder_insert(fire_at=(datetime.now(UTC) + timedelta(hours=4)).isoformat(), text="x", lead_minutes=0)
    item_id = db.accountability_insert(rid, frid, "fresh task")
    db.runtime_set("pending_accountability_check", str(item_id))

    out = _format_pending_accountability()
    assert out is not None
    assert "pending accountability check" in out.lower() or "fresh task" in out


@pytest.mark.asyncio
async def test_pending_accountability_ttl_expires_with_old_followup(monkeypatch):
    """Pending key with both created_at AND follow-up fire-at older than 48h → expire."""
    from agents.hooks import _format_pending_accountability

    rid = db.reminder_insert(fire_at=(datetime.now(UTC) - timedelta(hours=72)).isoformat(), text="x", lead_minutes=0)
    frid = db.reminder_insert(fire_at=(datetime.now(UTC) - timedelta(hours=50)).isoformat(), text="x", lead_minutes=0)
    item_id = db.accountability_insert(rid, frid, "ancient task")
    old = (datetime.now(UTC) - timedelta(hours=72)).isoformat()
    with db._conn() as c:
        c.execute("UPDATE accountability_items SET created_at = ? WHERE id = ?", (old, item_id))
    db.runtime_set("pending_accountability_check", str(item_id))

    out = _format_pending_accountability()
    assert out is None
    assert db.runtime_get("pending_accountability_check") is None


@pytest.mark.asyncio
async def test_pending_accountability_long_horizon_within_followup_ttl(monkeypatch):
    """check_after_minutes=4320 (3d): created_at >48h old but follow-up
    fire_at <48h. Should NOT expire."""
    from agents.hooks import _format_pending_accountability

    primary_iso = (datetime.now(UTC) - timedelta(hours=72)).isoformat()
    follow_iso = (datetime.now(UTC) - timedelta(hours=12)).isoformat()
    rid = db.reminder_insert(fire_at=primary_iso, text="x", lead_minutes=0)
    frid = db.reminder_insert(fire_at=follow_iso, text="x", lead_minutes=0)
    item_id = db.accountability_insert(rid, frid, "long-horizon task")
    with db._conn() as c:
        c.execute("UPDATE accountability_items SET created_at = ? WHERE id = ?", (primary_iso, item_id))
    db.runtime_set("pending_accountability_check", str(item_id))

    out = _format_pending_accountability()
    assert out is not None, "follow-up only fired 12h ago — should be in TTL window"
