"""Codex P1 regression: run_internal_control must never write to `messages`
and must never mutate session_id.

P1 contract for run_internal_control:
  - Hard contract: resume=None, no session_id writeback, no messages append,
    no handoff write. Returns text only.
  - Prompts passed to it (e.g. voice_critic rewrites, sync directives) must
    not appear as assistant rows in the conversation history.
"""
from __future__ import annotations

import importlib
from pathlib import Path

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
    yield


def _stub_invoke_sdk(monkeypatch, return_text: str = "OK"):
    """Patch _invoke_sdk so no real subprocess is spawned."""
    import agents.runtime as runtime_mod

    async def fake_invoke_sdk(prompt, *, resume, log_session_id, max_turns,
                               max_budget_usd, extra_allowed_tools=None,
                               retry_on_process_error=True,
                               inject_memory_enabled=True):
        # Mimic: log_session_id=False for internal control → no DB write.
        # We deliberately do NOT call db.set_session_id here to simulate the
        # contract that internal control never writes session_id.
        return return_text

    monkeypatch.setattr(runtime_mod, "_invoke_sdk", fake_invoke_sdk)
    return fake_invoke_sdk


@pytest.mark.asyncio
async def test_internal_control_prompt_not_in_messages(monkeypatch):
    """The prompt passed to run_internal_control must never appear in messages."""
    from agents.runtime import run_internal_control
    _stub_invoke_sdk(monkeypatch)

    internal_prompt = "[system: voice_critic flagged] rewrite this"
    await run_internal_control(internal_prompt)

    with db._conn() as c:
        rows = c.execute("SELECT content FROM messages").fetchall()
    contents = [r["content"] for r in rows]
    assert not any("[system:" in (c or "") for c in contents), (
        f"internal prompt leaked into messages: {contents}"
    )
    assert not any("voice_critic flagged" in (c or "") for c in contents), (
        f"voice_critic content leaked into messages: {contents}"
    )


@pytest.mark.asyncio
async def test_internal_control_return_not_in_messages(monkeypatch):
    """The return value of run_internal_control must not be appended to messages."""
    from agents.runtime import run_internal_control
    _stub_invoke_sdk(monkeypatch, return_text="INTERNAL_REPLY_XYZ")

    await run_internal_control("some internal directive")

    with db._conn() as c:
        rows = c.execute("SELECT content FROM messages").fetchall()
    contents = [r["content"] for r in rows]
    assert not any("INTERNAL_REPLY_XYZ" in (c or "") for c in contents), (
        f"internal reply leaked into messages: {contents}"
    )


@pytest.mark.asyncio
async def test_internal_control_does_not_mutate_session_id(monkeypatch):
    """run_internal_control must not overwrite the live session_id."""
    # Seed a live session.
    db.set_session_id("live-session-abc")

    from agents.runtime import run_internal_control

    # Stub _invoke_sdk — but even if it tried to write session_id, it's
    # guarded by log_session_id=False. Verify the guard holds.
    async def fake_invoke_sdk(prompt, *, resume, log_session_id, max_turns,
                               max_budget_usd, extra_allowed_tools=None,
                               retry_on_process_error=True,
                               inject_memory_enabled=True):
        # Sanity: internal control is called with log_session_id=False.
        assert log_session_id is False, (
            f"run_internal_control called _invoke_sdk with log_session_id={log_session_id!r}; "
            "expected False (stateless contract violated)"
        )
        return "internal result"

    import agents.runtime as runtime_mod
    monkeypatch.setattr(runtime_mod, "_invoke_sdk", fake_invoke_sdk)

    await run_internal_control("check something")

    assert db.get_session_id() == "live-session-abc", (
        "run_internal_control mutated the live session_id"
    )
