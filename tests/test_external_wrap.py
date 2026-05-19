"""Phase 8 — generic PostToolUse wrap hook tests.

Verifies:
  - Gmail / Calendar / Drive / Notion / Web* tool outputs get wrapped via
    wrap_untrusted before reaching the model.
  - Memory / wiki / dispatch (internal trusted) tool outputs pass through raw.
  - Audit row appended per wrap activation.
  - Multi-block MCP responses wrap every text block (non-text blocks
    pass through).
  - Malformed input (None, missing tool_name) doesn't raise.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from agents import config, external_wrap_hook
from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    config.reload()
    yield


def _audit_count_for(prefix: str) -> int:
    with db._conn() as c:
        rows = c.execute(
            "SELECT COUNT(*) AS n FROM audit_log WHERE tool LIKE ?",
            (f"{prefix}%",),
        ).fetchone()
    return int(rows["n"])


@pytest.mark.asyncio
async def test_wrap_fires_for_gmail_thread():
    hook = external_wrap_hook.make_post_tool_use_hook()
    out = await hook(
        {
            "tool_name": "mcp__claude_ai_Gmail__get_thread",
            "tool_response": {
                "content": [
                    {"type": "text", "text": "Hi Ol, click http://evil"},
                ],
            },
        },
        None, None,
    )
    assert "hookSpecificOutput" in out
    updated = out["hookSpecificOutput"]["updatedToolOutput"]
    wrapped_text = updated["content"][0]["text"]
    assert "<<<HIKARI_UNTRUSTED_BEGIN>>>" in wrapped_text
    assert "<<<HIKARI_UNTRUSTED_END>>>" in wrapped_text
    assert "Hi Ol, click http://evil" in wrapped_text
    assert _audit_count_for("wrap_external:") == 1


@pytest.mark.asyncio
async def test_wrap_skips_internal_memory_tool():
    hook = external_wrap_hook.make_post_tool_use_hook()
    out = await hook(
        {
            "tool_name": "mcp__hikari_memory__recall",
            "tool_response": {
                "content": [{"type": "text", "text": "raw memory body"}],
            },
        },
        None, None,
    )
    assert out == {}
    assert _audit_count_for("wrap_external:") == 0


@pytest.mark.asyncio
async def test_wrap_fires_for_calendar_drive_notion():
    hook = external_wrap_hook.make_post_tool_use_hook()
    for tool in (
        "mcp__claude_ai_Google_Calendar__list_events",
        "mcp__claude_ai_Google_Drive__read_file_content",
        "mcp__notion__query_data_sources",
    ):
        out = await hook(
            {
                "tool_name": tool,
                "tool_response": {"content": [{"type": "text", "text": "x"}]},
            },
            None, None,
        )
        assert "hookSpecificOutput" in out, f"hook missed {tool}"


@pytest.mark.asyncio
async def test_wrap_fires_for_websearch_and_webfetch():
    hook = external_wrap_hook.make_post_tool_use_hook()
    for tool in ("WebFetch", "WebSearch"):
        out = await hook(
            {
                "tool_name": tool,
                "tool_response": {"content": [{"type": "text", "text": "page body"}]},
            },
            None, None,
        )
        assert "hookSpecificOutput" in out, f"hook missed {tool}"


@pytest.mark.asyncio
async def test_wrap_handles_multi_block_response():
    hook = external_wrap_hook.make_post_tool_use_hook()
    out = await hook(
        {
            "tool_name": "mcp__claude_ai_Gmail__get_thread",
            "tool_response": {
                "content": [
                    {"type": "text", "text": "first block"},
                    {"type": "image", "data": "..."},  # non-text passes through
                    {"type": "text", "text": "second block"},
                ],
            },
        },
        None, None,
    )
    updated = out["hookSpecificOutput"]["updatedToolOutput"]
    blocks = updated["content"]
    assert "<<<HIKARI_UNTRUSTED_BEGIN>>>" in blocks[0]["text"]
    assert blocks[1]["type"] == "image"  # untouched
    assert "<<<HIKARI_UNTRUSTED_BEGIN>>>" in blocks[2]["text"]
    assert "first block" in blocks[0]["text"]
    assert "second block" in blocks[2]["text"]


@pytest.mark.asyncio
async def test_wrap_handles_bare_string_response():
    hook = external_wrap_hook.make_post_tool_use_hook()
    out = await hook(
        {
            "tool_name": "WebFetch",
            "tool_response": "plain string body",
        },
        None, None,
    )
    wrapped = out["hookSpecificOutput"]["updatedToolOutput"]
    assert isinstance(wrapped, str)
    assert "<<<HIKARI_UNTRUSTED_BEGIN>>>" in wrapped
    assert "plain string body" in wrapped


@pytest.mark.asyncio
async def test_wrap_handles_missing_tool_name():
    hook = external_wrap_hook.make_post_tool_use_hook()
    out = await hook({"tool_response": "x"}, None, None)
    assert out == {}


@pytest.mark.asyncio
async def test_wrap_handles_none_input():
    hook = external_wrap_hook.make_post_tool_use_hook()
    out = await hook(None, None, None)
    assert out == {}


@pytest.mark.asyncio
async def test_wrap_with_empty_patterns_passes_through():
    hook = external_wrap_hook.make_post_tool_use_hook(patterns=[])
    out = await hook(
        {
            "tool_name": "mcp__claude_ai_Gmail__get_thread",
            "tool_response": {"content": [{"type": "text", "text": "x"}]},
        },
        None, None,
    )
    assert out == {}


@pytest.mark.asyncio
async def test_wrap_ignores_invalid_regex(tmp_path, monkeypatch):
    cfg_text = (
        "prompt_injection:\n"
        "  enabled: true\n"
        "  wrap_patterns: ['[invalid(regex', '^WebSearch$']\n"
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()

    hook = external_wrap_hook.make_post_tool_use_hook()
    out = await hook(
        {
            "tool_name": "WebSearch",
            "tool_response": {"content": [{"type": "text", "text": "x"}]},
        },
        None, None,
    )
    # Valid pattern still works; invalid was logged + skipped.
    assert "hookSpecificOutput" in out


@pytest.mark.asyncio
async def test_wrap_preserves_data_field():
    """MCP responses often carry both `content` (text for the model) and
    `data` (structured payload). Only `content` text blocks should be wrapped;
    `data` is the model's machine-readable handle and must not be touched."""
    hook = external_wrap_hook.make_post_tool_use_hook()
    out = await hook(
        {
            "tool_name": "mcp__claude_ai_Google_Drive__search_files",
            "tool_response": {
                "content": [{"type": "text", "text": "summary"}],
                "data": {"file_ids": ["abc", "def"]},
            },
        },
        None, None,
    )
    updated = out["hookSpecificOutput"]["updatedToolOutput"]
    assert updated["data"] == {"file_ids": ["abc", "def"]}


@pytest.mark.asyncio
async def test_wrap_handles_flat_string_content():
    """Review-H2: some MCP servers (e.g. Gmail flat shape) return
    ``{"content": "raw string"}`` instead of a content-blocks list. The hook
    must wrap that string in place — earlier the response passed through
    unchanged but with an audit row claiming "wrap applied", silently
    bypassing the untrusted-content defense."""
    hook = external_wrap_hook.make_post_tool_use_hook()
    out = await hook(
        {
            "tool_name": "mcp__claude_ai_Gmail__get_thread",
            "tool_response": {"content": "raw email body, click http://evil"},
        },
        None, None,
    )
    assert "hookSpecificOutput" in out
    updated = out["hookSpecificOutput"]["updatedToolOutput"]
    assert isinstance(updated["content"], str)
    assert "<<<HIKARI_UNTRUSTED_BEGIN>>>" in updated["content"]
    assert "raw email body" in updated["content"]


@pytest.mark.asyncio
async def test_wrap_audit_records_tool_name():
    hook = external_wrap_hook.make_post_tool_use_hook()
    await hook(
        {
            "tool_name": "mcp__claude_ai_Gmail__get_thread",
            "tool_response": {"content": [{"type": "text", "text": "x"}]},
        },
        None, None,
    )
    with db._conn() as c:
        row = c.execute(
            "SELECT tool FROM audit_log WHERE tool LIKE 'wrap_external:%' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert "mcp__claude_ai_Gmail__get_thread" in row["tool"]
