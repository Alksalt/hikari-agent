"""Phase E tests: Gatekeeper state machine.

Tests: request → resolve(approved/rejected), deadline expiry, restart_recovery.
"""

from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

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


@pytest.fixture()
def gatekeeper():
    """Fresh Gatekeeper instance per test (not the module singleton)."""
    from tools.gatekeeper import Gatekeeper
    return Gatekeeper()


# ---------- request → resolve(approved) ----------

@pytest.mark.asyncio
async def test_request_approved(gatekeeper):
    sent: list[tuple[int, str]] = []

    async def fake_send(chat_id, text):
        sent.append((chat_id, text))

    gatekeeper.set_send_text(fake_send)
    # Trigger schema before writing.
    db.upsert_core_block("ping", "pong")

    deadline = datetime.now(timezone.utc) + timedelta(seconds=30)

    async def _resolve_soon():
        await asyncio.sleep(0.05)
        await gatekeeper.resolve("tu_approve_001", "approved")

    task = asyncio.create_task(_resolve_soon())
    outcome = await gatekeeper.request(
        tool_use_id="tu_approve_001",
        tool_name="mcp__google_workspace__gmail_bulk_delete_messages",
        chat_id=12345,
        args={"query": "label:trash"},
        summary="delete trash",
        deadline=deadline,
    )
    await task
    assert outcome == "approved"
    # DB row should be approved.
    with db._conn() as c:
        row = c.execute(
            "SELECT status FROM approvals WHERE tool_use_id = 'tu_approve_001'"
        ).fetchone()
    assert row["status"] == "approved"
    # Telegram prompt was sent.
    assert len(sent) == 1
    assert "delete trash" in sent[0][1]


# ---------- request → resolve(rejected) ----------

@pytest.mark.asyncio
async def test_request_rejected(gatekeeper):
    gatekeeper.set_send_text(AsyncMock())
    db.upsert_core_block("ping", "pong")

    deadline = datetime.now(timezone.utc) + timedelta(seconds=30)

    async def _reject_soon():
        await asyncio.sleep(0.05)
        await gatekeeper.resolve("tu_reject_001", "rejected")

    task = asyncio.create_task(_reject_soon())
    outcome = await gatekeeper.request(
        tool_use_id="tu_reject_001",
        tool_name="mcp__google_workspace__gmail_bulk_delete_messages",
        chat_id=12345,
        args={},
        summary="reject test",
        deadline=deadline,
    )
    await task
    assert outcome == "rejected"
    with db._conn() as c:
        row = c.execute(
            "SELECT status FROM approvals WHERE tool_use_id = 'tu_reject_001'"
        ).fetchone()
    assert row["status"] == "rejected"


# ---------- deadline expiry ----------

@pytest.mark.asyncio
async def test_request_expires_on_deadline(gatekeeper):
    gatekeeper.set_send_text(AsyncMock())
    db.upsert_core_block("ping", "pong")

    # Very short deadline.
    deadline = datetime.now(timezone.utc) + timedelta(milliseconds=50)
    outcome = await gatekeeper.request(
        tool_use_id="tu_expire_001",
        tool_name="some_tool",
        chat_id=12345,
        args={},
        summary="expiry test",
        deadline=deadline,
    )
    assert outcome == "expired"
    with db._conn() as c:
        row = c.execute(
            "SELECT status FROM approvals WHERE tool_use_id = 'tu_expire_001'"
        ).fetchone()
    # 'expired' is stored as 'timeout' in the DB (schema constraint).
    assert row["status"] == "timeout"


# ---------- idempotency: same tool_use_id called twice ----------

@pytest.mark.asyncio
async def test_request_idempotent_same_use_id(gatekeeper):
    """Two concurrent calls with the same tool_use_id share one event."""
    gatekeeper.set_send_text(AsyncMock())
    db.upsert_core_block("ping", "pong")

    deadline = datetime.now(timezone.utc) + timedelta(seconds=30)

    async def _approve():
        await asyncio.sleep(0.05)
        await gatekeeper.resolve("tu_idem_001", "approved")

    task = asyncio.create_task(_approve())

    # Launch both requests concurrently.
    results = await asyncio.gather(
        gatekeeper.request(
            tool_use_id="tu_idem_001", tool_name="t", chat_id=12345,
            args={}, summary="first", deadline=deadline,
        ),
        gatekeeper.request(
            tool_use_id="tu_idem_001", tool_name="t", chat_id=12345,
            args={}, summary="second", deadline=deadline,
        ),
    )
    await task
    assert all(r == "approved" for r in results)


# ---------- restart_recovery: stale rows expired, survivors nudged ----------

@pytest.mark.asyncio
async def test_restart_recovery_expires_stale_rows(gatekeeper):
    db.upsert_core_block("ping", "pong")

    nudge_calls: list[tuple[int, str]] = []

    async def fake_send(chat_id, text):
        nudge_calls.append((chat_id, text))

    gatekeeper.set_send_text(fake_send)

    # Insert a stale gatekeeper row (old created_at).
    with db._conn() as c:
        c.execute(
            "INSERT INTO approvals "
            "(chat_id, tool_name, tier, summary, args_json, status, created_at, "
            " tool_use_id, gate_kind) "
            "VALUES (12345, 'old_tool', 2, 'stale', '{}', 'pending', '2020-01-01T00:00:00', "
            " 'tu_stale_rr', 'gatekeeper')"
        )

    count = await gatekeeper.restart_recovery()
    assert count >= 1
    # Stale row is now 'timeout'.
    with db._conn() as c:
        row = c.execute(
            "SELECT status FROM approvals WHERE tool_use_id = 'tu_stale_rr'"
        ).fetchone()
    assert row["status"] == "timeout"


@pytest.mark.asyncio
async def test_restart_recovery_nudges_survivors(gatekeeper):
    """A very-recent pending row survives the stale cutoff; it gets nudged then expired."""
    db.upsert_core_block("ping", "pong")

    nudge_calls: list[tuple[int, str]] = []

    async def fake_send(chat_id, text):
        nudge_calls.append((chat_id, text))

    gatekeeper.set_send_text(fake_send)

    # Fresh row (created_at = now, won't be caught by 1h cutoff).
    db.approval_create_gatekeeper(
        chat_id=12345,
        tool_name="fresh_tool",
        tool_use_id="tu_fresh_rr",
        args_json="{}",
        summary="fresh",
        deadline_iso="2099-01-01T00:00:00+00:00",
    )

    count = await gatekeeper.restart_recovery()
    # The fresh row is still returned as a survivor and gets nudged + expired.
    assert count >= 1
    # Nudge message was sent.
    assert any("fresh_tool" in msg for _, msg in nudge_calls)
    # Fresh row is now 'timeout'.
    with db._conn() as c:
        row = c.execute(
            "SELECT status FROM approvals WHERE tool_use_id = 'tu_fresh_rr'"
        ).fetchone()
    assert row["status"] == "timeout"


# ---------- resolve returns False for unknown tool_use_id ----------

@pytest.mark.asyncio
async def test_resolve_unknown_use_id_returns_false(gatekeeper):
    db.upsert_core_block("ping", "pong")
    result = await gatekeeper.resolve("nonexistent_id", "approved")
    assert result is False


# ---------- Fix 1: race — timeout does not overwrite concurrent approve ----------

@pytest.mark.asyncio
async def test_timeout_does_not_overwrite_concurrent_approve(gatekeeper):
    """If outcome is already set to 'approved', the timeout-expire path must not
    overwrite it. Simulates the race by pre-setting outcome on the _Pending object
    before calling _resolve_internal with 'expired'."""
    from tools.gatekeeper import _Pending

    db.upsert_core_block("ping", "pong")

    # Create a real DB row so _resolve_internal can find it.
    aid = db.approval_create_gatekeeper(
        chat_id=12345,
        tool_name="some_tool",
        tool_use_id="tu_race_001",
        args_json="{}",
        summary="race test",
        deadline_iso="2099-01-01T00:00:00+00:00",
    )

    # Manually inject a _Pending with outcome already set to 'approved'.
    import asyncio as _asyncio
    pending = _Pending(
        aid=aid,
        chat_id=12345,
        tool_use_id="tu_race_001",
        tool_name="some_tool",
    )
    pending.outcome = "approved"
    pending.event.set()
    gatekeeper._by_use_id["tu_race_001"] = pending

    # Mark the DB row approved (simulating the approve path ran first).
    db.approval_resolve(aid, "approved")

    # Now call the timeout-expire path — it must be a no-op.
    await gatekeeper._resolve_internal("tu_race_001", "expired")

    # outcome on the in-memory object must still be 'approved'.
    assert pending.outcome == "approved"

    # DB row must still show 'approved', not 'timeout'.
    with db._conn() as c:
        row = c.execute(
            "SELECT status FROM approvals WHERE tool_use_id = 'tu_race_001'"
        ).fetchone()
    assert row["status"] == "approved"


# ---------- Fix 3: approved gatekeeper call writes audit row ----------

@pytest.mark.asyncio
async def test_gatekeeper_approve_writes_audit_row(gatekeeper):
    """After approval, _resolve_internal must append a hash-chained audit row."""
    gatekeeper.set_send_text(AsyncMock())
    db.upsert_core_block("ping", "pong")

    deadline = datetime.now(timezone.utc) + timedelta(seconds=30)

    async def _approve():
        await asyncio.sleep(0.05)
        await gatekeeper.resolve("tu_audit_001", "approved")

    task = asyncio.create_task(_approve())
    outcome = await gatekeeper.request(
        tool_use_id="tu_audit_001",
        tool_name="mcp__google_workspace__gmail_bulk_delete_messages",
        chat_id=12345,
        args={"query": "label:trash"},
        summary="audit row test",
        deadline=deadline,
    )
    await task
    assert outcome == "approved"

    # An audit_log row must exist for this tool call.
    with db._conn() as c:
        row = c.execute(
            "SELECT tool, approved_by, result_summary FROM audit_log "
            "WHERE tool = 'mcp__google_workspace__gmail_bulk_delete_messages' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None, "audit_append was not called after gatekeeper approval"
    assert row["approved_by"] == "owner"
    assert "gatekeeper approved" in (row["result_summary"] or "")
