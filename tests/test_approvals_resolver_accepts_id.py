"""9B: resolver accepts CONFIRM-SEND <id> and REJECT <id>."""
from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr("storage.db._DB_PATH", db_path)
    yield


def _make_approval(chat_id: int = 12345, tool_use_id: str = "tuid-1") -> int:
    from storage import db
    return db.approval_create_gatekeeper(
        chat_id=chat_id,
        tool_name="some_tool",
        tool_use_id=tool_use_id,
        args_json="{}",
        summary="do the thing",
        deadline_iso="2099-01-01T00:00:00+00:00",
        gate_kind="gatekeeper",
    )


@pytest.mark.asyncio
async def test_resolver_accepts_confirm_with_id():
    from storage import db
    row_id = _make_approval()
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


@pytest.mark.asyncio
async def test_resolver_reject_with_id():
    from storage import db
    row_id = _make_approval(tool_use_id="tuid-2")
    row = db.approval_get(row_id)
    tool_use_id = row["tool_use_id"]

    resolve_mock = AsyncMock(return_value=True)
    with patch("tools.approvals._BOT_REF", MagicMock()), \
         patch("tools.gatekeeper.GATEKEEPER") as gk_mock:
        gk_mock.resolve = resolve_mock
        from tools import approvals
        importlib.reload(approvals)
        from tools.approvals import resolve_pending_approval
        consumed = await resolve_pending_approval(12345, f"REJECT {row_id}")

    assert consumed is True
    resolve_mock.assert_awaited_once_with(tool_use_id, "rejected")


@pytest.mark.asyncio
async def test_resolver_id_mismatch_consumed_and_notifies():
    """Explicit id that belongs to a different chat sends error and returns True."""
    from storage import db
    row_id = _make_approval(chat_id=99999, tool_use_id="tuid-3")  # different chat

    safe_send_mock = AsyncMock()
    with patch("tools.approvals._safe_send", safe_send_mock), \
         patch("tools.approvals._BOT_REF", MagicMock()):
        from tools.approvals import resolve_pending_approval
        consumed = await resolve_pending_approval(12345, f"CONFIRM-SEND {row_id}")

    assert consumed is True
    safe_send_mock.assert_awaited_once()
    sent_text = safe_send_mock.call_args[0][1]
    assert "not yours" in sent_text


@pytest.mark.asyncio
async def test_resolver_bare_confirm_falls_back_to_most_recent():
    """CONFIRM-SEND without id uses approval_pending_for (most recent)."""
    _make_approval(tool_use_id="tuid-4")

    from storage import db
    pending = db.approval_pending_for(12345)
    assert pending is not None
    tool_use_id = pending["tool_use_id"]

    resolve_mock = AsyncMock(return_value=True)
    with patch("tools.approvals._BOT_REF", MagicMock()), \
         patch("tools.gatekeeper.GATEKEEPER") as gk_mock:
        gk_mock.resolve = resolve_mock
        from tools.approvals import resolve_pending_approval
        consumed = await resolve_pending_approval(12345, "CONFIRM-SEND")

    assert consumed is True
    resolve_mock.assert_awaited_once_with(tool_use_id, "approved")
