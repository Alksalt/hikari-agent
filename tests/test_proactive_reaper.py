"""Tests for agents.proactive_reaper (Sprint 7A).

Covers:
  - stale reserved row flips to aborted
  - fresh (recent) reserved row is left untouched
  - non-reserved status rows are untouched
"""
from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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
    yield
    db._reset_schema_sentinel()


def _insert_proactive_event(status: str, age_seconds: int) -> int:
    """Insert a proactive_events row with a synthetic sent_at in the past."""
    sent_at = (
        datetime.now(UTC) - timedelta(seconds=age_seconds)
    ).isoformat()
    with db._conn() as c:
        cur = c.execute(
            "INSERT INTO proactive_events "
            "(sent_at, source, pattern, payload_json, status) "
            "VALUES (?, 'test', 'test', '{}', ?)",
            (sent_at, status),
        )
    return cur.lastrowid


def _get_status(row_id: int) -> str:
    with db._conn() as c:
        row = c.execute(
            "SELECT status FROM proactive_events WHERE id = ?", (row_id,)
        ).fetchone()
    return row["status"] if row else "missing"


@pytest.mark.asyncio
async def test_stale_reserved_row_flips_to_aborted():
    """A row reserved 30s ago (> 10s threshold) must be flipped to aborted."""
    from agents.proactive_reaper import reap_stale_reservations
    row_id = _insert_proactive_event(status="reserved", age_seconds=30)
    count = await reap_stale_reservations()
    assert count == 1
    assert _get_status(row_id) == "aborted"


@pytest.mark.asyncio
async def test_fresh_reserved_row_untouched():
    """A row reserved 2s ago (< 10s threshold) must NOT be flipped."""
    from agents.proactive_reaper import reap_stale_reservations
    row_id = _insert_proactive_event(status="reserved", age_seconds=2)
    count = await reap_stale_reservations()
    assert count == 0
    assert _get_status(row_id) == "reserved"


@pytest.mark.asyncio
async def test_non_reserved_status_untouched():
    """Rows with status='sent' or 'aborted' must NOT be touched by the reaper."""
    from agents.proactive_reaper import reap_stale_reservations
    sent_id = _insert_proactive_event(status="sent", age_seconds=300)
    aborted_id = _insert_proactive_event(status="aborted", age_seconds=300)
    count = await reap_stale_reservations()
    assert count == 0
    assert _get_status(sent_id) == "sent"
    assert _get_status(aborted_id) == "aborted"


@pytest.mark.asyncio
async def test_multiple_stale_rows_all_flipped():
    """Multiple stale reserved rows are all flipped in one call."""
    from agents.proactive_reaper import reap_stale_reservations
    ids = [_insert_proactive_event(status="reserved", age_seconds=200) for _ in range(3)]
    count = await reap_stale_reservations()
    assert count == 3
    for row_id in ids:
        assert _get_status(row_id) == "aborted"
