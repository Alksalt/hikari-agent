"""tests/test_gatekeeper_deny_logging.py — Task 8 observability regression guard.

The 7x validation-loop incident (2026-07-04) left zero traces: the
registry-miss deny in ``gatekeeper_can_use_tool`` was silent, and
``log_tool_failure`` (PostToolUseFailure hook) only logged — it wrote no
``tool_calls`` telemetry row. Both paths must now be observable:
  1. An unknown tool name denied by the gatekeeper logs a WARNING that
     names the tool and the reason.
  2. A hook-reported tool failure writes a ``tool_calls`` row with
     ``error_class="HookReportedFailure"`` so it shows up in telemetry
     queries even though the tool handler itself never ran.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
from pathlib import Path

import pytest

from tools.gatekeeper_can_use_tool import gatekeeper_can_use_tool

# ---------------------------------------------------------------------------
# Isolation fixture — mirrors tests/test_phase_c_tool_gate.py /
# tests/test_tool_registry.py: fresh on-disk sqlite DB per test, schema
# sentinel reset so migrations rerun against it.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from storage import db
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield


def test_unknown_tool_deny_is_logged(caplog):
    with caplog.at_level(logging.WARNING):
        result = asyncio.run(
            gatekeeper_can_use_tool("mcp__nope__ghost_tool", {}, object())
        )
    assert "ghost_tool" in caplog.text
    assert "not found in tool registry" in caplog.text
    assert type(result).__name__ == "PermissionResultDeny"


def test_log_tool_failure_writes_telemetry():
    from agents.hooks import log_tool_failure
    from storage import db

    asyncio.run(log_tool_failure(
        {"tool_name": "mcp__hikari_utility__reminder_create",
         "error": "Input validation error: 'when_iso' is a required property"},
        "toolu_test", object(),
    ))
    with db._conn() as c:
        row = c.execute(
            "SELECT tool_id, success, error_class FROM tool_calls "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["tool_id"] == "mcp__hikari_utility__reminder_create"
    assert row["success"] == 0
    assert row["error_class"] == "HookReportedFailure"
