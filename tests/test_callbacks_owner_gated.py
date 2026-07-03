"""9B: callback dispatcher is owner-gated and routes correctly."""
from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "99999")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    # Patch the DB_PATH on both the reloaded module AND the original reference
    # held by already-imported modules (e.g. agents.telegram_bridge.db).
    from storage import db as _db_orig
    monkeypatch.setattr(_db_orig, "_DB_PATH", db_path)
    monkeypatch.setattr(_db_mod, "_DB_PATH", db_path)
    _db_orig._reset_schema_sentinel()
    # Also patch owner_id in telegram_bridge so the owner check uses 99999.
    import agents.telegram_bridge as _tb
    monkeypatch.setattr(_tb, "owner_id", lambda: 99999)
    yield
    _db_orig._reset_schema_sentinel()


def _make_update(user_id: int, callback_data: str):
    """Build a minimal Update mock with a callback_query."""
    query = MagicMock()
    query.from_user = MagicMock()
    query.from_user.id = user_id
    query.data = callback_data
    query.answer = AsyncMock()
    query.message = MagicMock()
    query.message.chat_id = 99999
    update = MagicMock()
    update.callback_query = query
    return update


def _make_context(bot=None):
    ctx = MagicMock()
    ctx.bot = bot or AsyncMock()
    return ctx


@pytest.mark.asyncio
async def test_non_owner_callback_is_ignored():
    """A callback from a non-owner user must be dropped BEFORE answer() — the
    owner gate now runs first so a stranger's tap gets neither an ack nor a
    route."""
    from agents.telegram_bridge import _handle_callback

    update = _make_update(user_id=11111, callback_data="appr:confirm:1")
    ctx = _make_context()

    resolve_mock = AsyncMock(return_value=True)
    with patch("tools.gatekeeper.GATEKEEPER") as gk_mock:
        gk_mock.resolve = resolve_mock
        await _handle_callback(update, ctx)

    update.callback_query.answer.assert_not_awaited()
    resolve_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_approval_callback_routes():
    """Owner tapping reject must call GATEKEEPER.resolve with 'rejected'.
    Confirm is no longer an inline button — user must type CONFIRM-SEND <id>.
    """
    from agents.telegram_bridge import _handle_callback
    from storage import db

    row_id = db.approval_create_gatekeeper(
        chat_id=99999,
        tool_name="test_tool",
        tool_use_id="tuid-cb-1",
        args_json="{}",
        summary="test",
        deadline_iso="2099-01-01T00:00:00+00:00",
        gate_kind="gatekeeper",
    )
    update = _make_update(user_id=99999, callback_data=f"appr:reject:{row_id}")
    ctx = _make_context()

    resolve_mock = AsyncMock(return_value=True)
    with patch("tools.gatekeeper.GATEKEEPER") as gk_mock:
        gk_mock.resolve = resolve_mock
        await _handle_callback(update, ctx)

    resolve_mock.assert_awaited_once_with("tuid-cb-1", "rejected")


@pytest.mark.asyncio
async def test_unknown_namespace_is_logged_not_crashed():
    """An unknown callback namespace must not raise."""
    from agents.telegram_bridge import _handle_callback

    update = _make_update(user_id=99999, callback_data="unknown:whatever:123")
    ctx = _make_context()

    # Must not raise.
    await _handle_callback(update, ctx)
    update.callback_query.answer.assert_awaited_once()
