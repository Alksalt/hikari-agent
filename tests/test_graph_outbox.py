"""Sprint 5D — graph_outbox table + helpers.

11 test cases:
  1. schema: graph_outbox table and indexes created on fresh DB
  2. graph_outbox_insert basic: inserts a pending row, returns id
  3. graph_outbox_insert dedup: second insert for same source returns None
  4. graph_outbox_pending: returns rows ordered by created_at, status filter
  5. graph_outbox_mark_sent: status → 'sent', processed_at set
  6. graph_outbox_mark_failed: increments attempts; flips to 'failed' at 5
  7. graph_outbox_stats: zero-fills all four statuses
  8. insert_fact writes outbox row in same transaction
  9. bulk_insert_facts writes outbox rows for every inserted fact
 10. backfill script: idempotent — running twice gives same row count
 11. process_outbox: drains pending rows, marks sent/failed correctly

Uses the fresh-DB fixture pattern from test_entities_and_provenance.py.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from storage import db


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    """Fresh per-test DB — mirrors test_entities_and_provenance.py."""
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield


# ---------------------------------------------------------------------------
# 1. Schema — table and indexes created on fresh DB
# ---------------------------------------------------------------------------

def test_schema_graph_outbox_table_created():
    """graph_outbox table must exist with the expected columns."""
    with db._conn() as c:
        tables = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "graph_outbox" in tables, "graph_outbox table not created"
        cols = {r["name"] for r in c.execute("PRAGMA table_info(graph_outbox)").fetchall()}
    expected = {
        "id", "source_table", "source_id", "payload_json",
        "status", "attempts", "last_error", "created_at", "processed_at",
    }
    assert expected.issubset(cols)


def test_schema_graph_outbox_indexes_created():
    """Both outbox indexes must exist."""
    with db._conn() as c:
        indexes = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()}
    assert "idx_graph_outbox_status_created" in indexes
    assert "idx_graph_outbox_source" in indexes


# ---------------------------------------------------------------------------
# 2. graph_outbox_insert — basic insert
# ---------------------------------------------------------------------------

def test_graph_outbox_insert_basic():
    """Insert a row; returned id is a positive int; status defaults to 'pending'."""
    payload = json.dumps({"v": 1, "episode_body": "test fact"})
    row_id = db.graph_outbox_insert("facts", 99, payload)
    assert row_id is not None and row_id > 0
    rows = db.graph_outbox_pending()
    assert len(rows) == 1
    assert rows[0]["source_table"] == "facts"
    assert rows[0]["source_id"] == 99
    assert rows[0]["status"] == "pending"
    assert rows[0]["attempts"] == 0


# ---------------------------------------------------------------------------
# 3. graph_outbox_insert dedup — second insert returns None
# ---------------------------------------------------------------------------

def test_graph_outbox_insert_dedup():
    """Inserting the same (source_table, source_id) twice returns None on the second call."""
    payload = json.dumps({"v": 1})
    first = db.graph_outbox_insert("facts", 42, payload)
    second = db.graph_outbox_insert("facts", 42, payload)
    assert first is not None
    assert second is None  # unique conflict → INSERT OR IGNORE → rowcount=0
    assert len(db.graph_outbox_pending()) == 1


# ---------------------------------------------------------------------------
# 4. graph_outbox_pending — ordering and status filter
# ---------------------------------------------------------------------------

def test_graph_outbox_pending_ordering():
    """Pending rows are returned ASC by created_at."""
    for i in range(3):
        db.graph_outbox_insert("facts", i + 1, json.dumps({"v": 1, "i": i}))
    rows = db.graph_outbox_pending()
    assert len(rows) == 3
    # All should be pending.
    assert all(r["status"] == "pending" for r in rows)
    # source_ids in insertion order (created_at is monotonic for sequential inserts).
    # The unique index guarantees rows are distinct; insertion order is preserved.
    source_ids = [r["source_id"] for r in rows]
    assert source_ids == sorted(source_ids), "rows should be ordered by created_at ASC"


def test_graph_outbox_pending_excludes_sent():
    """Sent and failed rows must not appear in graph_outbox_pending."""
    rid1 = db.graph_outbox_insert("facts", 1, json.dumps({"v": 1}))
    rid2 = db.graph_outbox_insert("facts", 2, json.dumps({"v": 1}))
    db.graph_outbox_mark_sent(rid1)
    rows = db.graph_outbox_pending()
    assert len(rows) == 1
    assert rows[0]["id"] == rid2


# ---------------------------------------------------------------------------
# 5. graph_outbox_mark_sent
# ---------------------------------------------------------------------------

def test_graph_outbox_mark_sent():
    """mark_sent sets status='sent' and processed_at."""
    rid = db.graph_outbox_insert("facts", 7, json.dumps({"v": 1}))
    db.graph_outbox_mark_sent(rid)
    with db._conn() as c:
        row = c.execute("SELECT * FROM graph_outbox WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "sent"
    assert row["processed_at"] is not None


# ---------------------------------------------------------------------------
# 6. graph_outbox_mark_failed — attempts counter and status flip at 5
# ---------------------------------------------------------------------------

def test_graph_outbox_mark_failed_increments():
    """First 4 mark_failed calls keep status='pending'; 5th flips to 'failed'."""
    rid = db.graph_outbox_insert("facts", 55, json.dumps({"v": 1}))
    for i in range(4):
        db.graph_outbox_mark_failed(rid, f"err {i}")
    with db._conn() as c:
        row = c.execute("SELECT * FROM graph_outbox WHERE id=?", (rid,)).fetchone()
    assert row["attempts"] == 4
    assert row["status"] == "pending", "should still be pending after 4 failures"
    # 5th failure flips to failed.
    db.graph_outbox_mark_failed(rid, "err 4")
    with db._conn() as c:
        row = c.execute("SELECT * FROM graph_outbox WHERE id=?", (rid,)).fetchone()
    assert row["attempts"] == 5
    assert row["status"] == "failed"
    assert "err 4" in row["last_error"]


# ---------------------------------------------------------------------------
# 7. graph_outbox_stats — zero-fills all four statuses
# ---------------------------------------------------------------------------

def test_graph_outbox_stats_zero_filled():
    """stats returns all statuses (including drained) even when the table is empty."""
    stats = db.graph_outbox_stats()
    assert set(stats.keys()) == {"pending", "sent", "failed", "skipped", "drained"}
    assert all(v == 0 for v in stats.values())


def test_graph_outbox_stats_counts():
    """stats counts are accurate after inserts, mark_sent, mark_failed."""
    rid1 = db.graph_outbox_insert("facts", 1, json.dumps({"v": 1}))
    rid2 = db.graph_outbox_insert("facts", 2, json.dumps({"v": 1}))
    db.graph_outbox_insert("facts", 3, json.dumps({"v": 1}))
    db.graph_outbox_mark_sent(rid1)
    for _ in range(5):
        db.graph_outbox_mark_failed(rid2, "bad")
    stats = db.graph_outbox_stats()
    assert stats["pending"] == 1
    assert stats["sent"] == 1
    assert stats["failed"] == 1
    assert stats["skipped"] == 0


# ---------------------------------------------------------------------------
# 8. insert_fact writes outbox row in same transaction
# ---------------------------------------------------------------------------

def test_insert_fact_writes_outbox_row():
    """insert_fact must write a pending outbox row in the same transaction."""
    fid = db.insert_fact("user", "likes", "coffee")
    pending = db.graph_outbox_pending()
    assert len(pending) == 1
    row = pending[0]
    assert row["source_table"] == "facts"
    assert row["source_id"] == fid
    payload = json.loads(row["payload_json"])
    assert payload["v"] == 1
    assert "coffee" in payload["episode_body"]


# ---------------------------------------------------------------------------
# 9. bulk_insert_facts writes outbox rows for every fact
# ---------------------------------------------------------------------------

def test_bulk_insert_facts_writes_outbox_rows():
    """bulk_insert_facts must produce one pending outbox row per inserted fact."""
    rows = [
        {"subject": "user", "predicate": "likes", "object": f"thing_{i}"}
        for i in range(5)
    ]
    n = db.bulk_insert_facts(rows)
    assert n == 5
    pending = db.graph_outbox_pending()
    assert len(pending) == 5
    assert all(r["source_table"] == "facts" for r in pending)


# ---------------------------------------------------------------------------
# 10. backfill script: idempotent
# ---------------------------------------------------------------------------

def test_backfill_script_idempotent(tmp_path, monkeypatch):
    """Running backfill twice inserts the same number of rows as the first pass."""
    import sys

    # insert some facts without outbox rows by patching graph_outbox_insert
    # to a no-op during insert_fact so the outbox stays empty first.
    original_insert = db.graph_outbox_insert

    def _no_op_insert(*args, **kwargs):
        return None

    # Insert facts while suppressing the outbox.
    monkeypatch.setattr(db, "graph_outbox_insert", _no_op_insert)
    db.insert_fact("user", "knows", "python")
    db.insert_fact("user", "uses", "vim")
    monkeypatch.setattr(db, "graph_outbox_insert", original_insert)

    # Now run backfill — should insert 2 rows.
    _REPO_ROOT = Path(__file__).resolve().parent.parent
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))

    import scripts.backfill_graph_outbox as _bf
    importlib.reload(_bf)

    count1 = _bf.backfill(dry_run=False)
    assert count1 == 2

    # Second run — idempotent, should insert 0 new rows.
    count2 = _bf.backfill(dry_run=False)
    assert count2 == 0


# ---------------------------------------------------------------------------
# 11. process_outbox: drains pending rows, marks sent/failed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_process_outbox_marks_sent():
    """process_outbox marks rows sent when add_episode_safe returns True."""
    db.insert_fact("user", "knows", "testing")
    db.insert_fact("user", "likes", "pytest")

    # Patch add_episode_safe to succeed.
    with patch("storage.graph.add_episode_safe", new=AsyncMock(return_value=True)):
        import storage.graph as _graph
        stats = await _graph.process_outbox(limit=50, max_per_call=10)

    assert stats["polled"] == 2
    assert stats["sent"] == 2
    assert stats["failed"] == 0
    assert db.graph_outbox_pending() == []


@pytest.mark.asyncio
async def test_process_outbox_marks_failed_on_false():
    """process_outbox increments failure when add_episode_safe returns False."""
    db.insert_fact("user", "knows", "failure")

    with patch("storage.graph.add_episode_safe", new=AsyncMock(return_value=False)):
        import storage.graph as _graph
        stats = await _graph.process_outbox(limit=50, max_per_call=10)

    assert stats["polled"] == 1
    assert stats["failed"] == 1
    assert stats["sent"] == 0
    # Row should still be pending (only 1 attempt so far, threshold is 5).
    pending = db.graph_outbox_pending()
    assert len(pending) == 1
    assert pending[0]["attempts"] == 1
