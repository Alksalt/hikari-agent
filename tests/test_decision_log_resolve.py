"""decision_log_resolve MCP tool — outcome recording and idempotency."""
from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    yield


def test_resolve_records_outcome_one():
    from storage import db
    from tools.decision_log.resolve import decision_log_resolve
    did = db.decision_insert("ships friday", 0.8, "2026-05-01")
    asyncio.run(decision_log_resolve.handler({"decision_id": did, "outcome": 1}))
    with db._conn() as c:
        row = c.execute(
            "SELECT outcome, resolved_at FROM decisions WHERE id = ?", (did,)
        ).fetchone()
    assert row["outcome"] == 1
    assert row["resolved_at"] is not None


def test_resolve_records_outcome_zero():
    from storage import db
    from tools.decision_log.resolve import decision_log_resolve
    did = db.decision_insert("ships friday", 0.8, "2026-05-01")
    asyncio.run(decision_log_resolve.handler({"decision_id": did, "outcome": 0}))
    with db._conn() as c:
        row = c.execute(
            "SELECT outcome FROM decisions WHERE id = ?", (did,)
        ).fetchone()
    assert row["outcome"] == 0


def test_resolve_rejects_invalid_outcome():
    from storage import db
    from tools.decision_log.resolve import decision_log_resolve
    did = db.decision_insert("ships friday", 0.8, "2026-05-01")
    result = asyncio.run(
        decision_log_resolve.handler({"decision_id": did, "outcome": 2})
    )
    assert "must be 0 or 1" in str(result)
    with db._conn() as c:
        row = c.execute(
            "SELECT outcome FROM decisions WHERE id = ?", (did,)
        ).fetchone()
    assert row["outcome"] is None


def test_resolve_idempotent_on_same_outcome():
    from storage import db
    from tools.decision_log.resolve import decision_log_resolve
    did = db.decision_insert("ships friday", 0.8, "2026-05-01")
    asyncio.run(decision_log_resolve.handler({"decision_id": did, "outcome": 1}))
    result = asyncio.run(
        decision_log_resolve.handler({"decision_id": did, "outcome": 1})
    )
    assert result is not None
    with db._conn() as c:
        row = c.execute(
            "SELECT outcome FROM decisions WHERE id = ?", (did,)
        ).fetchone()
    assert row["outcome"] == 1


def test_resolve_refuses_different_outcome_overwrite():
    """Calibration ledger must be immutable against prompt-injected revisions."""
    from storage import db
    from tools.decision_log.resolve import decision_log_resolve
    did = db.decision_insert("ships friday", 0.8, "2026-05-01")
    asyncio.run(decision_log_resolve.handler({"decision_id": did, "outcome": 1}))
    result = asyncio.run(
        decision_log_resolve.handler({"decision_id": did, "outcome": 0})
    )
    assert "already resolved" in str(result) or "refusing" in str(result)
    with db._conn() as c:
        row = c.execute(
            "SELECT outcome FROM decisions WHERE id = ?", (did,)
        ).fetchone()
    assert row["outcome"] == 1


def test_resolve_writes_audit_log_row():
    """Every successful resolve must leave a forensic trail."""
    from storage import db
    from tools.decision_log.resolve import decision_log_resolve
    did = db.decision_insert("ships friday", 0.8, "2026-05-01")
    asyncio.run(decision_log_resolve.handler({"decision_id": did, "outcome": 1}))
    with db._conn() as c:
        rows = c.execute(
            "SELECT tool, args_json_redacted, approved_by FROM audit_log "
            "WHERE tool = 'decision_resolve'"
        ).fetchall()
    assert len(rows) == 1
    assert str(did) in rows[0]["args_json_redacted"]
    assert rows[0]["approved_by"] == "owner"


def test_resolve_raises_on_nonexistent_decision():
    from tools.decision_log.resolve import decision_log_resolve
    result = asyncio.run(
        decision_log_resolve.handler({"decision_id": 99999, "outcome": 1})
    )
    assert "not found" in str(result)
