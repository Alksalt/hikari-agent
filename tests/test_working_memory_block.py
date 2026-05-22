"""working_memory block — _format_working_memory + inject_memory integration."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from agents import config
from storage import db


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    config.reload()
    yield


def _insert_chat(role: str, content: str) -> int:
    return db.append_message(role, content, source="chat")


def test_working_memory_returns_last_k_turns():
    from agents.hooks import _format_working_memory

    for i in range(10):
        role = "user" if i % 2 == 0 else "assistant"
        _insert_chat(role, f"message {i}")

    result = _format_working_memory(k=6)
    assert result.startswith("# working_memory")
    lines = [l for l in result.splitlines() if l.startswith("you:") or l.startswith("hikari:")]
    # 10 inserted, last dropped (current user turn), take last 6 of remaining 9 → 6 lines
    assert len(lines) == 6
    assert "you:" in result or "hikari:" in result


def test_working_memory_truncates_long_snippets():
    from agents.hooks import _format_working_memory

    long_content = "x" * 1000
    _insert_chat("user", long_content)
    _insert_chat("assistant", "reply")

    result = _format_working_memory(k=6)
    lines = [l for l in result.splitlines() if l.startswith("you:")]
    assert lines, "expected at least one 'you:' line"
    content_part = lines[0][len("you: "):]
    assert len(content_part) == 400


def test_working_memory_drops_current_turn_user_row():
    from agents.hooks import _format_working_memory

    _insert_chat("user", "earlier message")
    _insert_chat("assistant", "reply to earlier")
    _insert_chat("user", "THIS IS THE CURRENT TURN")

    result = _format_working_memory(k=6)
    assert "THIS IS THE CURRENT TURN" not in result
    assert "earlier message" in result or "reply to earlier" in result


def test_working_memory_disabled_returns_empty(monkeypatch):
    from agents import config as cfg_mod

    original_get = cfg_mod.get

    def patched_get(key, default=None):
        if key == "working_memory.enabled":
            return False
        return original_get(key, default)

    monkeypatch.setattr(cfg_mod, "get", patched_get)

    from agents import hooks
    importlib.reload(hooks)

    result = hooks._format_working_memory()
    assert result == ""

    importlib.reload(hooks)


def test_inject_memory_includes_working_memory():
    from agents.hooks import inject_memory

    _insert_chat("user", "hello from the past")
    _insert_chat("assistant", "hikari said something")
    _insert_chat("user", "current turn")

    result = import_and_run(inject_memory)
    ctx = result.get("hookSpecificOutput", {}).get("additionalContext", "")

    assert "# now" in ctx
    assert "# working_memory" in ctx

    now_pos = ctx.index("# now")
    wm_pos = ctx.index("# working_memory")
    assert now_pos < wm_pos, "working_memory must come after # now"

    if "# memory: core" in ctx:
        core_pos = ctx.index("# memory: core")
        assert wm_pos < core_pos, "working_memory must come before # memory: core"


def import_and_run(fn):
    import asyncio
    return asyncio.run(fn({}, None, None))
