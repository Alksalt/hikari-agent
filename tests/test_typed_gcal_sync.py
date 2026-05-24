"""Tests for the typed sync_gcal_reminder adapter (Sprint 7B scope C).

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
# _sync_gcal_reminder success path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sync_gcal_success_persists_event_id():
    from tools.reminders.sync_gcal import _sync_gcal_reminder

    mock_result = {"id": "gcal-event-abc"}
    with (
        patch("tools.reminders.sync_gcal.MANAGER") as mock_mgr,
        patch("tools.reminders.sync_gcal.db") as mock_db,
    ):
        mock_mgr.call = AsyncMock(return_value=mock_result)
        result = await _sync_gcal_reminder(
            reminder_id=99,
            title="Vet appointment",
            start_iso="2026-05-24T14:00:00Z",
        )

    assert result.reminder_id == 99
    assert result.gcal_event_id == "gcal-event-abc"
    mock_db.reminder_update_gcal_event.assert_called_once_with(99, "gcal-event-abc")


@pytest.mark.asyncio
async def test_sync_gcal_mcp_error_raises():
    from tools.reminders.sync_gcal import _sync_gcal_reminder

    with patch("tools.reminders.sync_gcal.MANAGER") as mock_mgr:
        mock_mgr.call = AsyncMock(
            side_effect=McpCallError("google_workspace", "create_calendar_event", "quota exceeded")
        )
        with pytest.raises(McpCallError) as exc_info:
            await _sync_gcal_reminder(99, "Vet appointment", "2026-05-24T14:00:00Z")

    assert "quota exceeded" in str(exc_info.value)


@pytest.mark.asyncio
async def test_sync_gcal_empty_event_id_raises_mcp_error():
    from tools.reminders.sync_gcal import _sync_gcal_reminder

    with (
        patch("tools.reminders.sync_gcal.MANAGER") as mock_mgr,
        patch("tools.reminders.sync_gcal.db"),
    ):
        mock_mgr.call = AsyncMock(return_value={"text": "{}"})
        with pytest.raises(McpCallError):
            await _sync_gcal_reminder(99, "Vet appointment", "2026-05-24T14:00:00Z")


def test_no_run_internal_control_in_adapter():
    import inspect

    import tools.reminders.sync_gcal as mod
    src = inspect.getsource(mod)
    assert "run_internal_control" not in src
