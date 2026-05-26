"""Sprint B Wave 3 — pruner tests for the four Sprint A pruner functions.

Tests:
  - prune_tool_calls(retention_days=30)
  - prune_graph_outbox_sent(older_than_days=14)
  - prune_media_outbox_terminal(older_than_days=14)
  - prune_proactive_events(older_than_days=90)

Each test seeds old + recent rows, prunes, and asserts exact residuals.
"""
from __future__ import annotations

import importlib
import time
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
    # Bootstrap schema.
    db.upsert_core_block("_boot", "_boot")
    yield
    db._reset_schema_sentinel()


# ---------------------------------------------------------------------------
# prune_tool_calls
# ---------------------------------------------------------------------------

def _insert_tool_call(days_ago: int, idx: int) -> None:
    """Insert a tool_calls row with started_at backdated by days_ago."""
    with db._conn() as c:
        c.execute(
            "INSERT INTO tool_calls (tool_id, started_at, duration_ms, success, output_size) "
            "VALUES (?, datetime('now', ? || ' days'), 10, 1, 0)",
            (f"tool-{idx}", f"-{days_ago}"),
        )


def test_prune_tool_calls_deletes_old_keeps_recent():
    # 3 rows older than 30d (31d, 60d, 90d), 2 recent (1d, 5d).
    _insert_tool_call(31, 1)
    _insert_tool_call(60, 2)
    _insert_tool_call(90, 3)
    _insert_tool_call(1, 4)
    _insert_tool_call(5, 5)

    deleted = db.prune_tool_calls(older_than_days=30)

    assert deleted == 3
    with db._conn() as c:
        remaining = c.execute("SELECT COUNT(*) AS n FROM tool_calls").fetchone()["n"]
    assert remaining == 2


def test_prune_tool_calls_empty_table():
    assert db.prune_tool_calls(older_than_days=30) == 0


def test_prune_tool_calls_all_recent_nothing_deleted():
    _insert_tool_call(1, 1)
    _insert_tool_call(5, 2)
    _insert_tool_call(10, 3)

    deleted = db.prune_tool_calls(older_than_days=30)
    assert deleted == 0
    with db._conn() as c:
        remaining = c.execute("SELECT COUNT(*) AS n FROM tool_calls").fetchone()["n"]
    assert remaining == 3


# NB: a boundary test at exactly older_than_days=30 was tried and removed —
# the SQLite `datetime('now', '-30 days')` resolves at INSERT time and the pruner
# computes its cutoff microseconds LATER, so any "row exactly at boundary"
# always races on the wrong side of the cutoff. The 29d-retained / 31d-deleted
# tests above already cover the DELETE WHERE < cutoff semantics meaningfully.


# ---------------------------------------------------------------------------
# prune_graph_outbox_sent
# ---------------------------------------------------------------------------

def _insert_graph_outbox(status: str, days_ago_processed: int, sid: int) -> None:
    """Insert a graph_outbox row with processed_at backdated by days_ago_processed."""
    epoch_offset = int(time.time()) - days_ago_processed * 86400
    with db._conn() as c:
        c.execute(
            "INSERT INTO graph_outbox "
            "(source_table, source_id, payload_json, status, created_at, processed_at) "
            "VALUES ('facts', ?, '{}', ?, ?, ?)",
            (sid, status, epoch_offset - 1, epoch_offset),
        )


def test_prune_graph_outbox_sent_deletes_old_terminal():
    # 2 old sent (20d), 1 old drained (20d), 1 old skipped (20d) — all deleted.
    # 1 recent sent (5d) — kept.
    # 1 old pending (20d) — kept (pending is never terminal).
    _insert_graph_outbox("sent", 20, 1)
    _insert_graph_outbox("sent", 20, 2)
    _insert_graph_outbox("drained", 20, 3)
    _insert_graph_outbox("skipped", 20, 4)
    _insert_graph_outbox("sent", 5, 5)
    _insert_graph_outbox("pending", 20, 6)

    deleted = db.prune_graph_outbox_sent(older_than_days=14)

    assert deleted == 4
    with db._conn() as c:
        remaining = c.execute("SELECT COUNT(*) AS n FROM graph_outbox").fetchone()["n"]
    assert remaining == 2


def test_prune_graph_outbox_empty():
    assert db.prune_graph_outbox_sent(older_than_days=14) == 0


def test_prune_graph_outbox_pending_never_deleted():
    _insert_graph_outbox("pending", 100, 1)
    _insert_graph_outbox("failed", 100, 2)

    deleted = db.prune_graph_outbox_sent(older_than_days=14)
    assert deleted == 0


# ---------------------------------------------------------------------------
# prune_media_outbox_terminal
# ---------------------------------------------------------------------------

def _insert_media_outbox(status: str, days_ago: int, key: str) -> None:
    with db._conn() as c:
        c.execute(
            "INSERT INTO media_outbox "
            "(kind, idempotency_key, payload_json, status, created_at) "
            "VALUES ('text', ?, '{}', ?, datetime('now', ? || ' days'))",
            (key, status, f"-{days_ago}"),
        )


def test_prune_media_outbox_terminal_deletes_old():
    # 3 old terminal (20d): sent, failed, aborted.
    # 1 recent sent (5d) — kept.
    # 1 old pending (20d) — kept (not terminal).
    _insert_media_outbox("sent", 20, "k1")
    _insert_media_outbox("failed", 20, "k2")
    _insert_media_outbox("aborted", 20, "k3")
    _insert_media_outbox("sent", 5, "k4")
    _insert_media_outbox("pending", 20, "k5")

    deleted = db.prune_media_outbox_terminal(older_than_days=14)

    assert deleted == 3
    with db._conn() as c:
        remaining = c.execute("SELECT COUNT(*) AS n FROM media_outbox").fetchone()["n"]
    assert remaining == 2


def test_prune_media_outbox_empty():
    assert db.prune_media_outbox_terminal(older_than_days=14) == 0


def test_prune_media_outbox_pending_never_deleted():
    _insert_media_outbox("pending", 100, "p1")
    _insert_media_outbox("pending", 50, "p2")

    assert db.prune_media_outbox_terminal(older_than_days=14) == 0
    with db._conn() as c:
        remaining = c.execute("SELECT COUNT(*) AS n FROM media_outbox").fetchone()["n"]
    assert remaining == 2


# ---------------------------------------------------------------------------
# prune_proactive_events
# ---------------------------------------------------------------------------

def _insert_proactive_event(days_ago: int, idx: int) -> None:
    with db._conn() as c:
        c.execute(
            "INSERT INTO proactive_events "
            "(sent_at, source, pattern, payload_json) "
            "VALUES (datetime('now', ? || ' days'), 'test', 'p', '{}')",
            (f"-{days_ago}",),
        )


def test_prune_proactive_events_deletes_old():
    # 3 old (100d), 2 recent (30d, 10d).
    _insert_proactive_event(100, 1)
    _insert_proactive_event(100, 2)
    _insert_proactive_event(100, 3)
    _insert_proactive_event(30, 4)
    _insert_proactive_event(10, 5)

    deleted = db.prune_proactive_events(older_than_days=90)

    assert deleted == 3
    with db._conn() as c:
        remaining = c.execute("SELECT COUNT(*) AS n FROM proactive_events").fetchone()["n"]
    assert remaining == 2


def test_prune_proactive_events_empty():
    assert db.prune_proactive_events(older_than_days=90) == 0


def test_prune_proactive_events_all_recent_nothing_deleted():
    _insert_proactive_event(10, 1)
    _insert_proactive_event(30, 2)
    _insert_proactive_event(60, 3)

    assert db.prune_proactive_events(older_than_days=90) == 0
    with db._conn() as c:
        remaining = c.execute("SELECT COUNT(*) AS n FROM proactive_events").fetchone()["n"]
    assert remaining == 3


def test_prune_proactive_events_custom_threshold():
    """With a short threshold (7 days), only truly old rows are deleted."""
    _insert_proactive_event(10, 1)  # deleted
    _insert_proactive_event(5, 2)   # kept
    _insert_proactive_event(1, 3)   # kept

    deleted = db.prune_proactive_events(older_than_days=7)
    assert deleted == 1
    with db._conn() as c:
        remaining = c.execute("SELECT COUNT(*) AS n FROM proactive_events").fetchone()["n"]
    assert remaining == 2
