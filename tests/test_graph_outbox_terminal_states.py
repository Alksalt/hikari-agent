"""Graph outbox terminal-state distinction — transient vs permanent.

Invariants:
  1. OPENROUTER_API_KEY missing → process_outbox keeps rows 'pending' (skipped).
  2. GRAPHITI_ENABLED=false → process_outbox returns 0 polled immediately.
  3. graph_outbox_mark_failed with transient error string keeps row 'pending'.
  4. graph_outbox_mark_failed with permanent error flips to 'failed' at 5 attempts.
  5. graph_outbox_failed_stats returns failed count + last_error.
  6. fact_id is embedded in outbox payload by schedule_episode.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from storage import db


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield


# ---------------------------------------------------------------------------
# 1. OPENROUTER_API_KEY missing → rows stay pending (process_outbox skips)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_api_key_keeps_rows_pending(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("GRAPHITI_ENABLED", "true")

    fid = db.insert_fact("user", "likes", "coffee")
    pending_before = db.graph_outbox_pending()
    assert len(pending_before) == 1

    import storage.graph as _graph
    stats = await _graph.process_outbox(limit=50, max_per_call=10)

    # polled=1, sent=0, failed=0, skipped=1
    assert stats["sent"] == 0
    assert stats["failed"] == 0

    # Row must still be 'pending' (not 'failed')
    with db._conn() as c:
        row = c.execute("SELECT * FROM graph_outbox WHERE source_id=?", (fid,)).fetchone()
    assert row["status"] == "pending", f"expected pending, got {row['status']!r}"


# ---------------------------------------------------------------------------
# 2. GRAPHITI_ENABLED=false → process_outbox returns immediately (0 polled)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_graphiti_disabled_process_outbox_noop(monkeypatch):
    monkeypatch.setenv("GRAPHITI_ENABLED", "false")
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy")

    db.insert_fact("user", "has", "data")
    import storage.graph as _graph
    stats = await _graph.process_outbox(limit=50, max_per_call=10)

    assert stats == {"polled": 0, "sent": 0, "failed": 0, "skipped": 0}

    with db._conn() as c:
        row = c.execute("SELECT status FROM graph_outbox LIMIT 1").fetchone()
    assert row["status"] == "pending"


# ---------------------------------------------------------------------------
# 3. Transient error string keeps row 'pending'
# ---------------------------------------------------------------------------

def test_mark_failed_transient_keeps_pending():
    rid = db.graph_outbox_insert("facts", 99, json.dumps({"v": 1}))
    db.graph_outbox_mark_failed(rid, "OPENROUTER_API_KEY not set (transient)")

    with db._conn() as c:
        row = c.execute("SELECT * FROM graph_outbox WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "pending"
    assert row["attempts"] == 1


def test_mark_failed_transient_graphiti_disabled_keeps_pending():
    rid = db.graph_outbox_insert("facts", 100, json.dumps({"v": 1}))
    db.graph_outbox_mark_failed(rid, "GRAPHITI_ENABLED=false, skipping")

    with db._conn() as c:
        row = c.execute("SELECT * FROM graph_outbox WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "pending"
    assert row["attempts"] == 1


# ---------------------------------------------------------------------------
# 4. Permanent error flips to 'failed' at 5 attempts
# ---------------------------------------------------------------------------

def test_mark_failed_permanent_flips_at_5():
    rid = db.graph_outbox_insert("facts", 55, json.dumps({"v": 1}))
    for i in range(4):
        db.graph_outbox_mark_failed(rid, f"payload_json invalid: err {i}")
    with db._conn() as c:
        row = c.execute("SELECT * FROM graph_outbox WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "pending"
    assert row["attempts"] == 4

    db.graph_outbox_mark_failed(rid, "payload_json invalid: err 4")
    with db._conn() as c:
        row = c.execute("SELECT * FROM graph_outbox WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "failed"
    assert row["attempts"] == 5


# ---------------------------------------------------------------------------
# 5. graph_outbox_failed_stats returns count + last_error
# ---------------------------------------------------------------------------

def test_graph_outbox_failed_stats_empty():
    stats = db.graph_outbox_failed_stats()
    assert stats["count"] == 0
    assert stats["last_error"] is None


def test_graph_outbox_failed_stats_with_failed_rows():
    rid = db.graph_outbox_insert("facts", 77, json.dumps({"v": 1}))
    for i in range(5):
        db.graph_outbox_mark_failed(rid, f"bad payload {i}")

    stats = db.graph_outbox_failed_stats()
    assert stats["count"] == 1
    assert stats["last_error"] is not None
    assert "bad payload" in stats["last_error"]


# ---------------------------------------------------------------------------
# 6. fact_id is embedded in outbox payload by schedule_episode
# ---------------------------------------------------------------------------

def test_insert_fact_payload_embeds_fact_id():
    import json
    fid = db.insert_fact("user", "likes", "tea")
    rows = db.graph_outbox_pending()
    matching = [r for r in rows if r.get("source_id") == fid]
    assert matching, "insert_fact did not create an outbox row"
    payload = json.loads(matching[0]["payload_json"])
    assert payload.get("fact_id") == fid, (
        f"insert_fact outbox payload missing fact_id; got {payload!r}"
    )


@pytest.mark.uses_real_graph
def test_schedule_episode_embeds_fact_id():
    """schedule_episode payload must include the SQLite fact_id.

    Marked uses_real_graph to bypass the conftest's schedule_episode mock so
    the real function body (which writes to graph_outbox) executes.
    """
    from storage import graph as _graph

    source_id = 42
    rid = _graph.schedule_episode(
        name="fact_42",
        episode_body="user reads books",
        source_id=source_id,
        source_description="fact",
    )
    assert rid is not None, "schedule_episode returned None — outbox insert failed"

    rows = db.graph_outbox_pending()
    assert len(rows) == 1, f"expected 1 pending row, got {len(rows)}"
    payload = json.loads(rows[0]["payload_json"])
    assert payload.get("fact_id") == source_id, (
        f"expected fact_id={source_id} in payload, got {payload!r}"
    )


# ---------------------------------------------------------------------------
# 7. "add_episode_safe returned False" is NOT transient — permanent path
# ---------------------------------------------------------------------------
#
# _TRANSIENT_PREFIXES = ("OPENROUTER_API_KEY", "GRAPHITI_ENABLED").
# The string "add_episode_safe returned False" does NOT match either prefix,
# so it must follow the permanent-error path: status stays 'pending' until
# attempts reaches 5, then flips to 'failed'.

def test_add_episode_safe_false_error_is_not_transient_before_threshold():
    """4 marks with the False-return error stay pending (permanent but < threshold)."""
    rid = db.graph_outbox_insert("facts", 200, json.dumps({"v": 1}))
    for _ in range(4):
        db.graph_outbox_mark_failed(rid, "add_episode_safe returned False")

    with db._conn() as c:
        row = c.execute("SELECT status, attempts FROM graph_outbox WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "pending", (
        f"expected pending before threshold, got {row['status']!r}"
    )
    assert row["attempts"] == 4


def test_add_episode_safe_false_error_flips_to_failed_at_threshold():
    """5th mark with the False-return error must flip status to 'failed'."""
    rid = db.graph_outbox_insert("facts", 201, json.dumps({"v": 1}))
    for _ in range(5):
        db.graph_outbox_mark_failed(rid, "add_episode_safe returned False")

    with db._conn() as c:
        row = c.execute("SELECT status, attempts, last_error FROM graph_outbox WHERE id=?",
                        (rid,)).fetchone()
    assert row["status"] == "failed", (
        f"expected failed at threshold, got {row['status']!r}"
    )
    assert row["attempts"] == 5
    assert "add_episode_safe returned False" in row["last_error"]


async def test_process_outbox_add_episode_false_marks_row_via_permanent_path(monkeypatch):
    """process_outbox with add_episode_safe returning False writes the permanent
    error string, confirming the row is on the flip-at-5 path (not transient)."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy-key")
    monkeypatch.setenv("GRAPHITI_ENABLED", "true")

    # Insert a fact so there's a pending outbox row.
    db.insert_fact("user", "prefers", "dark mode")
    rows = db.graph_outbox_pending()
    assert rows, "expected at least one pending row after insert_fact"
    row_id = rows[0]["id"]

    # add_episode_safe is already patched to return False by the global conftest
    # fixture (_block_graphiti), so we can call process_outbox directly.
    import storage.graph as _graph
    stats = await _graph.process_outbox(limit=50, max_per_call=10)

    # process_outbox counts a False return as a failed attempt.
    assert stats["sent"] == 0
    assert stats["failed"] >= 1

    # The row should have its last_error set to the False-return message
    # and be on the permanent (not transient) track.
    with db._conn() as c:
        row = c.execute(
            "SELECT status, attempts, last_error FROM graph_outbox WHERE id=?",
            (row_id,),
        ).fetchone()
    # After 1 attempt it stays pending (threshold is 5).
    assert row["status"] == "pending", (
        f"expected pending after 1 failed attempt, got {row['status']!r}"
    )
    assert row["last_error"] is not None
    assert "add_episode_safe returned False" in row["last_error"]
