"""Tests for the typed sync_apple_reminder adapter (Sprint 7B scope C).

Mocks MANAGER.call and db helpers — no live MCP connections or DB required.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agents.mcp_manager import McpCallError


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from agents import config
    config.reload()
    yield


# ---------------------------------------------------------------------------
# _sync_apple_reminder success path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sync_apple_success_persists_event_id():
    from tools.reminders.sync_apple import _sync_apple_reminder

    mock_result = {"id": "apple-abc-123"}
    with (
        patch("tools.reminders.sync_apple.MANAGER") as mock_mgr,
        patch("tools.reminders.sync_apple.db") as mock_db,
    ):
        mock_mgr.call = AsyncMock(return_value=mock_result)
        result = await _sync_apple_reminder(
            reminder_id=42,
            title="Buy milk",
            due_iso="2026-05-24T10:00:00Z",
        )

    assert result.reminder_id == 42
    assert result.apple_event_id == "apple-abc-123"
    mock_db.reminder_update_apple_event.assert_called_once_with(42, "apple-abc-123")


@pytest.mark.asyncio
async def test_sync_apple_mcp_error_raises():
    from tools.reminders.sync_apple import _sync_apple_reminder

    with patch("tools.reminders.sync_apple.MANAGER") as mock_mgr:
        mock_mgr.call = AsyncMock(
            side_effect=McpCallError("apple_events", "reminders_tasks", "EventKit denied")
        )
        with pytest.raises(McpCallError) as exc_info:
            await _sync_apple_reminder(42, "Buy milk", "2026-05-24T10:00:00Z")

    assert "EventKit denied" in str(exc_info.value)


@pytest.mark.asyncio
async def test_sync_apple_empty_event_id_raises_mcp_error():
    """If the MCP result contains no recognisable id, raise McpCallError."""
    from tools.reminders.sync_apple import _sync_apple_reminder

    with (
        patch("tools.reminders.sync_apple.MANAGER") as mock_mgr,
        patch("tools.reminders.sync_apple.db"),
    ):
        mock_mgr.call = AsyncMock(return_value={"text": "{}"})
        with pytest.raises(McpCallError):
            await _sync_apple_reminder(42, "Buy milk", "2026-05-24T10:00:00Z")


def test_no_run_internal_control_in_adapter():
    import inspect

    import tools.reminders.sync_apple as mod
    src = inspect.getsource(mod)
    assert "run_internal_control" not in src
