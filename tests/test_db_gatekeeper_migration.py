"""Phase E tests: _migrate_approvals_gatekeeper — new columns, partial indexes,
idempotency, and backfill from deferred_tool_use_id.
"""

from __future__ import annotations

import importlib
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
    from agents import config
    config.reload()
    yield


def _trigger_schema():
    """Any DB write triggers _conn → _ensure_schema → all migrations."""
    db.upsert_core_block("ping", "pong")


# ---------- column presence ----------

def test_gatekeeper_columns_present():
    _trigger_schema()
    with db._conn() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(approvals)").fetchall()}
    for col in ("tool_use_id", "deadline_iso", "executed_at", "result_summary", "gate_kind"):
        assert col in cols, f"missing column: {col}"


# ---------- idempotency ----------

def test_migration_is_idempotent():
    """Running the migration twice must not raise."""
    _trigger_schema()
    with db._conn() as c:
        db._migrate_approvals_gatekeeper(c)
    # Still queryable — no duplicate-column error.
    with db._conn() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(approvals)").fetchall()}
    assert "tool_use_id" in cols


# ---------- backfill from deferred_tool_use_id ----------

def test_backfill_from_deferred_tool_use_id():
    """tool_use_id is backfilled from deferred_tool_use_id for legacy rows.

    Since both columns are added in one migration pass, we simulate the pre-migration
    state by inserting a row with deferred_tool_use_id set and then running the
    backfill UPDATE directly — verifying the SQL logic is correct.
    """
    _trigger_schema()
    # Insert a row with deferred_tool_use_id (as if it existed pre-migration).
    with db._conn() as c:
        c.execute(
            "INSERT INTO approvals "
            "(chat_id, tool_name, tier, summary, args_json, status, created_at, "
            " deferred_tool_use_id) "
            "VALUES (1, 'old_tool', 2, 'test', '{}', 'pending', datetime('now'), 'tu_legacy')"
        )
        # Simulate the backfill step from the migration:
        c.execute(
            "UPDATE approvals SET tool_use_id = deferred_tool_use_id "
            "WHERE tool_use_id IS NULL AND deferred_tool_use_id IS NOT NULL"
        )
        row = c.execute(
            "SELECT tool_use_id FROM approvals WHERE deferred_tool_use_id = 'tu_legacy'"
        ).fetchone()
    assert row is not None
    assert row["tool_use_id"] == "tu_legacy"


# ---------- partial unique index: one pending per chat ----------

def test_partial_unique_index_one_pending_per_chat():
    """approvals_one_pending_per_chat: two GATEKEEPER pending rows with same chat_id → IntegrityError.

    The index is scoped to gate_kind='gatekeeper' so the legacy defer path
    (which has always allowed multiple pending rows) is unaffected.
    """
    import sqlite3
    _trigger_schema()
    db.approval_create_gatekeeper(
        chat_id=999,
        tool_name="tool_a",
        tool_use_id="tu_001",
        args_json="{}",
        summary="first",
        deadline_iso="2099-01-01T00:00:00+00:00",
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.approval_create_gatekeeper(
            chat_id=999,
            tool_name="tool_b",
            tool_use_id="tu_002",
            args_json="{}",
            summary="second (should fail — chat 999 already has a pending gatekeeper row)",
            deadline_iso="2099-01-01T00:00:00+00:00",
        )


def test_partial_unique_index_allows_multiple_legacy_pending():
    """Legacy defer rows (gate_kind IS NULL) may have multiple pending rows per chat."""
    _trigger_schema()
    # Insert two legacy (non-gatekeeper) pending rows for the same chat — must succeed.
    with db._conn() as c:
        c.execute(
            "INSERT INTO approvals "
            "(chat_id, tool_name, tier, summary, args_json, status, created_at) "
            "VALUES (777, 'legacy_a', 2, 'a', '{}', 'pending', datetime('now'))"
        )
        c.execute(
            "INSERT INTO approvals "
            "(chat_id, tool_name, tier, summary, args_json, status, created_at) "
            "VALUES (777, 'legacy_b', 2, 'b', '{}', 'pending', datetime('now'))"
        )
    with db._conn() as c:
        rows = c.execute(
            "SELECT COUNT(*) AS cnt FROM approvals WHERE chat_id = 777 AND status = 'pending'"
        ).fetchone()
    assert rows["cnt"] == 2


# ---------- partial unique index: one pending per tool_use_id ----------

def test_partial_unique_index_one_pending_per_use_id():
    """approvals_one_pending_per_use_id: two pending rows with same tool_use_id → IntegrityError."""
    import sqlite3
    _trigger_schema()
    db.approval_create_gatekeeper(
        chat_id=101,
        tool_name="tool_a",
        tool_use_id="tu_dup",
        args_json="{}",
        summary="first",
        deadline_iso="2099-01-01T00:00:00+00:00",
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.approval_create_gatekeeper(
            chat_id=102,  # different chat, same tool_use_id
            tool_name="tool_a",
            tool_use_id="tu_dup",
            args_json="{}",
            summary="second",
            deadline_iso="2099-01-01T00:00:00+00:00",
        )


# ---------- helper functions ----------

def test_approval_create_gatekeeper_round_trip():
    _trigger_schema()
    aid = db.approval_create_gatekeeper(
        chat_id=555,
        tool_name="mcp__google_workspace__gmail_bulk_delete_messages",
        tool_use_id="tu_gk_001",
        args_json='{"query": "label:trash"}',
        summary="gmail_bulk_delete: delete trash",
        deadline_iso="2099-12-31T23:59:59+00:00",
    )
    assert aid > 0
    row = db.approval_pending_by_use_id("tu_gk_001")
    assert row is not None
    assert row["gate_kind"] == "gatekeeper"
    assert row["tool_use_id"] == "tu_gk_001"
    assert row["chat_id"] == 555
    assert row["status"] == "pending"


def test_approval_pending_by_use_id_returns_none_when_missing():
    _trigger_schema()
    row = db.approval_pending_by_use_id("nonexistent_tool_use_id")
    assert row is None


def test_approval_mark_executed():
    _trigger_schema()
    aid = db.approval_create_gatekeeper(
        chat_id=666,
        tool_name="some_tool",
        tool_use_id="tu_exec_001",
        args_json="{}",
        summary="exec test",
        deadline_iso="2099-01-01T00:00:00+00:00",
    )
    db.approval_resolve(aid, "approved")
    db.approval_mark_executed(aid, result_summary="ran ok")
    with db._conn() as c:
        row = c.execute("SELECT * FROM approvals WHERE id = ?", (aid,)).fetchone()
    assert row["executed_at"] is not None
    assert row["result_summary"] == "ran ok"


def test_approval_expire_stale():
    _trigger_schema()
    # Insert a gatekeeper row with an old created_at.
    with db._conn() as c:
        c.execute(
            "INSERT INTO approvals "
            "(chat_id, tool_name, tier, summary, args_json, status, created_at, "
            " tool_use_id, gate_kind) "
            "VALUES (77, 'old_gk_tool', 2, 'old', '{}', 'pending', '2020-01-01T00:00:00', "
            " 'tu_stale', 'gatekeeper')"
        )
    count = db.approval_expire_stale("2025-01-01T00:00:00+00:00")
    assert count == 1
    with db._conn() as c:
        row = c.execute(
            "SELECT status FROM approvals WHERE tool_use_id = 'tu_stale'"
        ).fetchone()
    assert row["status"] == "timeout"


def test_approval_expire_stale_does_not_touch_defer_rows():
    """expire_stale must skip legacy defer rows (gate_kind IS NULL)."""
    _trigger_schema()
    with db._conn() as c:
        c.execute(
            "INSERT INTO approvals "
            "(chat_id, tool_name, tier, summary, args_json, status, created_at) "
            "VALUES (88, 'defer_tool', 2, 'defer test', '{}', 'pending', '2020-01-01T00:00:00')"
        )
    count = db.approval_expire_stale("2025-01-01T00:00:00+00:00")
    assert count == 0  # defer row not touched


def test_approvals_list_pending_gatekeeper():
    _trigger_schema()
    db.approval_create_gatekeeper(
        chat_id=123,
        tool_name="tool_x",
        tool_use_id="tu_list_001",
        args_json="{}",
        summary="list test",
        deadline_iso="2099-01-01T00:00:00+00:00",
    )
    rows = db.approvals_list_pending_gatekeeper()
    assert len(rows) == 1
    assert rows[0]["tool_use_id"] == "tu_list_001"
    assert rows[0]["gate_kind"] == "gatekeeper"
