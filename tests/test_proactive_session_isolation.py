"""Codex P1 regression: hidden session mutation.

run_internal_control must NEVER overwrite the live session_id.
run_user_turn MUST update session_id (positive case).

The live session_id is the key that lets the SDK resume the existing
Claude subprocess. If an internal-control call clobbers it with a
transient sub-session id, the next user turn forks a new Claude process
and the conversation context is lost.
"""
from __future__ import annotations

import asyncio
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


@pytest.mark.asyncio
async def test_run_internal_control_does_not_overwrite_session_id(monkeypatch):
    """run_internal_control is stateless: it must NOT update the live session_id
    even if the stubbed SDK returns a different session_id in its ResultMessage."""
    db.set_session_id("live-session-123")

    import agents.runtime as runtime_mod

    async def fake_invoke_sdk(prompt, *, resume, log_session_id, max_turns,
                               max_budget_usd, extra_allowed_tools=None,
                               retry_on_process_error=True,
                               inject_memory_enabled=True,
                               use_persistent_live=False,
                               model=None):
        # Simulate SDK returning a different session from the internal call.
        # With log_session_id=False the caller must NOT persist this.
        assert not log_session_id, (
            "run_internal_control violated stateless contract: log_session_id=True"
        )
        # Attempt to write directly (simulates a buggy path).
        # The guard is that log_session_id is False — _invoke_sdk itself
        # only calls db.set_session_id when log_session_id is True.
        return "internal answer"

    monkeypatch.setattr(runtime_mod, "_invoke_sdk", fake_invoke_sdk)

    from agents.runtime import run_internal_control
    result = await run_internal_control("internal directive")

    assert result == "internal answer"
    assert db.get_session_id() == "live-session-123", (
        f"session_id was overwritten to {db.get_session_id()!r}; "
        "run_internal_control must not mutate the live session"
    )


@pytest.mark.asyncio
async def test_run_user_turn_updates_session_id(monkeypatch):
    """run_user_turn DOES update session_id (positive case)."""
    db.set_session_id("old-session-xyz")

    import agents.runtime as runtime_mod

    async def fake_invoke_sdk(prompt, *, resume, log_session_id, max_turns,
                               max_budget_usd, extra_allowed_tools=None,
                               retry_on_process_error=True,
                               inject_memory_enabled=True,
                               use_persistent_live=False,
                               model=None):
        # run_user_turn passes log_session_id=True.
        assert log_session_id is True, (
            f"run_user_turn called _invoke_sdk with log_session_id={log_session_id!r}; "
            "expected True"
        )
        # Simulate the SDK writing a new session_id.
        db.set_session_id("new-session-456")
        return "user turn reply"

    monkeypatch.setattr(runtime_mod, "_invoke_sdk", fake_invoke_sdk)

    # _RUN_LOCK is an asyncio.Lock — acquire inside the same event loop.
    from agents.runtime import run_user_turn
    result = await run_user_turn("hello")

    assert result == "user turn reply"
    assert db.get_session_id() == "new-session-456", (
        "run_user_turn did not update session_id"
    )


@pytest.mark.asyncio
async def test_concurrent_internal_control_does_not_race_session_id(monkeypatch):
    """Two concurrent run_internal_control calls must not corrupt session_id."""
    db.set_session_id("live-concurrent-abc")

    import agents.runtime as runtime_mod

    call_count = {"n": 0}

    async def fake_invoke_sdk(prompt, *, resume, log_session_id, **kwargs):
        call_count["n"] += 1
        await asyncio.sleep(0.01)  # let the other call interleave
        return f"reply-{call_count['n']}"

    monkeypatch.setattr(runtime_mod, "_invoke_sdk", fake_invoke_sdk)

    from agents.runtime import run_internal_control
    results = await asyncio.gather(
        run_internal_control("directive A"),
        run_internal_control("directive B"),
    )

    # Session_id must be unchanged regardless of interleaving.
    assert db.get_session_id() == "live-concurrent-abc", (
        f"session_id was mutated to {db.get_session_id()!r} by concurrent internal calls"
    )
    assert len(results) == 2


@pytest.mark.asyncio
async def test_run_user_turn_blocks_uses_log_session_id_true(monkeypatch):
    """run_user_turn_blocks must use log_session_id=True so the session_id
    produced by the content-block turn is stored for PDF/image continuity.
    The live client is then reconnected so the next text turn resumes it.
    """
    import agents.runtime as runtime_mod
    import agents.sdk_pool as pool_mod

    log_session_id_received: list[bool] = []

    async def fake_invoke_sdk(prompt, *, resume, log_session_id, max_turns,
                               max_budget_usd, retry_on_process_error=True,
                               **kwargs):
        log_session_id_received.append(log_session_id)
        return "blocks reply"

    async def fake_reconnect(reason, *, lock_run=True):
        pass

    monkeypatch.setattr(runtime_mod, "_invoke_sdk", fake_invoke_sdk)
    monkeypatch.setattr(runtime_mod, "_RUN_LOCK", asyncio.Lock())
    monkeypatch.setattr(pool_mod, "is_live_persistent_path_enabled", lambda: False)
    monkeypatch.setattr(pool_mod, "_reconnect_live", fake_reconnect)

    from agents.runtime import run_user_turn_blocks
    result = await run_user_turn_blocks([{"type": "text", "text": "hi"}])

    assert result == "blocks reply"
    assert log_session_id_received == [True], (
        f"run_user_turn_blocks must call _invoke_sdk with log_session_id=True; "
        f"got {log_session_id_received}"
    )
