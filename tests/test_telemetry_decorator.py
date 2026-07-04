"""Tests for tools/_telemetry.py — per-tool invocation telemetry decorator."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from storage import db
from tools._telemetry import instrumented


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield


@pytest.mark.asyncio
async def test_success_row_written():
    """Happy path: a successful tool call writes one row with success=1."""
    @instrumented("my_tool")
    async def fake_tool(args):
        return {"content": [{"type": "text", "text": "hello world"}]}

    await fake_tool({})

    with db._conn() as c:
        rows = c.execute("SELECT * FROM tool_calls WHERE tool_id = 'my_tool'").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["success"] == 1
    assert row["error_class"] is None
    assert row["output_size"] == len("hello world")
    assert row["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_exception_row_written():
    """Exception path: success=0 and error_class is recorded; exception re-raised."""
    @instrumented("failing_tool")
    async def boom_tool(args):
        raise ValueError("intentional error")

    with pytest.raises(ValueError, match="intentional error"):
        await boom_tool({})

    with db._conn() as c:
        rows = c.execute("SELECT * FROM tool_calls WHERE tool_id = 'failing_tool'").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["success"] == 0
    assert row["error_class"] == "ValueError"


@pytest.mark.asyncio
async def test_multiple_calls_accumulate():
    """Each call appends its own row."""
    @instrumented("counter_tool")
    async def counter_tool(args):
        return {"content": [{"type": "text", "text": "x"}]}

    for _ in range(3):
        await counter_tool({})

    with db._conn() as c:
        count = c.execute(
            "SELECT COUNT(*) FROM tool_calls WHERE tool_id = 'counter_tool'"
        ).fetchone()[0]
    assert count == 3


@pytest.mark.asyncio
async def test_output_size_sums_text_blocks():
    """output_size is the sum of all text block lengths."""
    @instrumented("multi_block")
    async def multi_tool(args):
        return {"content": [
            {"type": "text", "text": "abc"},
            {"type": "text", "text": "de"},
        ]}

    await multi_tool({})

    with db._conn() as c:
        row = c.execute(
            "SELECT output_size FROM tool_calls WHERE tool_id = 'multi_block'"
        ).fetchone()
    assert row["output_size"] == 5  # len("abc") + len("de")


@pytest.mark.asyncio
async def test_oversized_output_logs_warning(caplog):
    """Outputs likely to blow the SDK's 25k-token MCP cap get a WARNING —
    otherwise the row says success=1 while the model saw only a size-limit
    error (the invisible 2026-07-04 jobhunt_radar failure mode)."""
    import tools._telemetry as tel

    @instrumented("fat_tool")
    async def fat_tool(args):
        return {"content": [{"type": "text", "text": "x" * (tel._OUTPUT_WARN_CHARS + 1)}]}

    with caplog.at_level("WARNING", logger="tools._telemetry"):
        await fat_tool({})

    assert any("fat_tool" in r.message and "cap" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_normal_output_no_warning(caplog):
    @instrumented("thin_tool")
    async def thin_tool(args):
        return {"content": [{"type": "text", "text": "small"}]}

    with caplog.at_level("WARNING", logger="tools._telemetry"):
        await thin_tool({})

    assert not caplog.records
