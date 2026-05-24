"""Tests for the typed calendar_get_events adapter (Sprint 7B scope C).

Mocks MANAGER.call — no live MCP connections required.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agents.mcp_manager import McpCallError
from tools.calendar.get_events import (
    CalendarEvent,
    _extract_events,
    _fetch_events,
    calendar_get_events,
)

# ---------------------------------------------------------------------------
# _extract_events unit tests
# ---------------------------------------------------------------------------

def test_extract_events_from_events_key():
    raw = {
        "events": [
            {"id": "abc", "title": "Meeting", "start": {"dateTime": "2026-05-24T10:00:00+02:00"},
             "end": {"dateTime": "2026-05-24T11:00:00+02:00"}},
        ]
    }
    items = _extract_events(raw)
    assert len(items) == 1
    assert items[0]["id"] == "abc"


def test_extract_events_from_text_json():
    import json
    payload = [{"id": "xyz", "summary": "Stand-up", "start": {}, "end": {}}]
    raw = {"text": json.dumps(payload)}
    items = _extract_events(raw)
    assert len(items) == 1
    assert items[0]["id"] == "xyz"


def test_extract_events_empty():
    assert _extract_events({}) == []
    assert _extract_events({"text": ""}) == []


# ---------------------------------------------------------------------------
# _fetch_events via mocked MANAGER
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_events_success():
    mock_result = {
        "events": [
            {
                "id": "ev1",
                "summary": "Dentist",
                "start": {"dateTime": "2026-05-24T09:00:00Z"},
                "end": {"dateTime": "2026-05-24T09:30:00Z"},
                "location": "Clinic",
            }
        ]
    }
    with patch("tools.calendar.get_events.MANAGER") as mock_mgr:
        mock_mgr.call = AsyncMock(return_value=mock_result)
        events = await _fetch_events("2026-05-24T00:00:00Z", "2026-05-24T23:59:59Z")

    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, CalendarEvent)
    assert ev.id == "ev1"
    assert ev.title == "Dentist"
    assert ev.location == "Clinic"
    mock_mgr.call.assert_awaited_once_with(
        "google_workspace",
        "calendar_get_events",
        {
            "time_min": "2026-05-24T00:00:00Z",
            "time_max": "2026-05-24T23:59:59Z",
            "calendar_id": "primary",
        },
    )


@pytest.mark.asyncio
async def test_fetch_events_mcp_error_propagates():
    with patch("tools.calendar.get_events.MANAGER") as mock_mgr:
        mock_mgr.call = AsyncMock(
            side_effect=McpCallError("google_workspace", "calendar_get_events", "timeout")
        )
        with pytest.raises(McpCallError) as exc_info:
            await _fetch_events("T", "T")
    assert "timeout" in str(exc_info.value)


@pytest.mark.asyncio
async def test_fetch_events_empty_result():
    with patch("tools.calendar.get_events.MANAGER") as mock_mgr:
        mock_mgr.call = AsyncMock(return_value={})
        events = await _fetch_events("T", "T")
    assert events == []


# ---------------------------------------------------------------------------
# calendar_get_events @tool handler
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_handler_success():
    mock_result = {
        "events": [
            {"id": "e1", "summary": "Lunch", "start": {}, "end": {}}
        ]
    }
    with patch("tools.calendar.get_events.MANAGER") as mock_mgr:
        mock_mgr.call = AsyncMock(return_value=mock_result)
        resp = await calendar_get_events.handler(
            {"time_min": "2026-05-24T00:00:00Z", "time_max": "2026-05-24T23:59:59Z"}
        )
    assert "fetched" in resp["content"][0]["text"]
    assert isinstance(resp["data"]["events"], list)


@pytest.mark.asyncio
async def test_tool_handler_mcp_error_returns_fail_envelope():
    with patch("tools.calendar.get_events.MANAGER") as mock_mgr:
        mock_mgr.call = AsyncMock(
            side_effect=McpCallError("google_workspace", "calendar_get_events", "auth denied")
        )
        resp = await calendar_get_events.handler(
            {"time_min": "T", "time_max": "T"}
        )
    assert "error" in resp["content"][0]["text"].lower()
    assert "_error" in resp["data"]


def test_no_run_internal_control_in_adapter():
    """Adapter must not use prompt-mediated LLM plumbing."""
    import inspect

    import tools.calendar.get_events as mod
    src = inspect.getsource(mod)
    assert "run_internal_control" not in src
