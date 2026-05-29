"""Phase 4 (control-plane-lies sweep) — apple_events on the gatekeeper state machine.

Tests:
- typed CONFIRM-SEND <id> approves an apple gatekeeper row
- typed REJECT <id> rejects an apple gatekeeper row
- /status pending count matches /approvals listed count after apple row inserted
- pending apple approval survives restart_recovery (nudged, then expires as timeout)
"""

from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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


def _make_apple_approval(
    chat_id: int = 12345,
    tool_name: str = "mcp__apple_events__create_reminder",
    tool_use_id: str = "tu_apple_1",
) -> int:
    """Insert a pending gatekeeper row for an apple_events write tool."""
    db.upsert_core_block("ping", "pong")  # ensure schema is initialised
    return db.approval_create_gatekeeper(
        chat_id=chat_id,
        tool_name=tool_name,
        tool_use_id=tool_use_id,
        args_json='{"title": "call dentist"}',
        summary="create_reminder: call dentist",
        deadline_iso="2099-01-01T00:00:00+00:00",
        gate_kind="gatekeeper",
    )


# ---------------------------------------------------------------------------
# Typed CONFIRM-SEND <id> approves an apple gatekeeper row
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_apple_approve_by_id():
    """CONFIRM-SEND <id> routes to gatekeeper.resolve(tool_use_id, 'approved')."""
    row_id = _make_apple_approval(tool_use_id="tu_apple_approve")
    row = db.approval_get(row_id)
    assert row is not None
    tool_use_id = row["tool_use_id"]

    resolve_mock = AsyncMock(return_value=True)
    with patch("tools.approvals._BOT_REF", MagicMock()), \
         patch("tools.gatekeeper.GATEKEEPER") as gk_mock:
        gk_mock.resolve = resolve_mock
        from tools.approvals import resolve_pending_approval
        consumed = await resolve_pending_approval(12345, f"CONFIRM-SEND {row_id}")

    assert consumed is True
    resolve_mock.assert_awaited_once_with(tool_use_id, "approved")


# ---------------------------------------------------------------------------
# Typed REJECT <id> rejects an apple gatekeeper row
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_apple_reject_by_id():
    """REJECT <id> routes to gatekeeper.resolve(tool_use_id, 'rejected')."""
    row_id = _make_apple_approval(tool_use_id="tu_apple_reject")
    row = db.approval_get(row_id)
    assert row is not None
    tool_use_id = row["tool_use_id"]

    resolve_mock = AsyncMock(return_value=True)
    with patch("tools.approvals._BOT_REF", MagicMock()), \
         patch("tools.gatekeeper.GATEKEEPER") as gk_mock:
        gk_mock.resolve = resolve_mock
        from tools.approvals import resolve_pending_approval
        consumed = await resolve_pending_approval(12345, f"REJECT {row_id}")

    assert consumed is True
    resolve_mock.assert_awaited_once_with(tool_use_id, "rejected")


# ---------------------------------------------------------------------------
# /status count == /approvals listed count
# ---------------------------------------------------------------------------

def test_status_count_matches_approvals_list():
    """/status pending count (scoped to gate_kind='gatekeeper') == len(approvals_list_pending_gatekeeper).

    Inserting one apple gatekeeper row means both sides should return 1.
    """
    _make_apple_approval(tool_use_id="tu_apple_status")

    # Replicate the cockpit /status query exactly (gate_kind scoped).
    with db._conn() as c:
        cockpit_count = c.execute(
            "SELECT COUNT(*) FROM approvals "
            "WHERE status='pending' AND gate_kind='gatekeeper'"
        ).fetchone()[0]

    listed = db.approvals_list_pending_gatekeeper()

    assert cockpit_count == len(listed), (
        f"/status count ({cockpit_count}) != /approvals listed ({len(listed)})"
    )
    assert cockpit_count == 1


# ---------------------------------------------------------------------------
# Pending apple approval survives restart_recovery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_apple_approval_survives_restart_recovery():
    """A fresh apple gatekeeper row appears in approvals_list_pending_gatekeeper,
    gets nudged, and ends with status='timeout' after restart_recovery."""
    from tools.gatekeeper import Gatekeeper

    _make_apple_approval(tool_use_id="tu_apple_restart")

    # Verify it's listed before recovery.
    pending = db.approvals_list_pending_gatekeeper()
    apple_rows = [r for r in pending if r["tool_name"] == "mcp__apple_events__create_reminder"]
    assert len(apple_rows) == 1, "Apple approval must appear in approvals_list_pending_gatekeeper"

    nudge_calls: list[tuple[int, str]] = []

    async def fake_send(chat_id, text):
        nudge_calls.append((chat_id, text))

    gk = Gatekeeper()
    gk.set_send_text(fake_send)

    count = await gk.restart_recovery()
    assert count >= 1

    # Nudge was sent mentioning the tool name.
    assert any("mcp__apple_events__create_reminder" in msg for _, msg in nudge_calls), (
        f"Expected nudge for apple tool; calls={nudge_calls}"
    )

    # Row is now timeout.
    with db._conn() as c:
        row = c.execute(
            "SELECT status FROM approvals WHERE tool_use_id = 'tu_apple_restart'"
        ).fetchone()
    assert row["status"] == "timeout", (
        f"Expected status='timeout' after restart_recovery; got {row['status']!r}"
    )

    # No longer listed as pending.
    post_pending = db.approvals_list_pending_gatekeeper()
    assert not any(r["tool_use_id"] == "tu_apple_restart" for r in post_pending)
