"""run_user_turn_blocks: after a content-block turn the live client must reconnect.

run_user_turn_blocks uses log_session_id=True so the new session_id is stored,
then calls sdk_pool._reconnect_live(..., lock_run=False) so the next text turn
can reference the PDF/image content.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from storage import db


# ---------------------------------------------------------------------------
# DB isolation
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    yield


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reconnect_called_when_persistent_enabled():
    """_reconnect_live is called once with lock_run=False after a successful block turn."""
    reconnect_calls: list[dict] = []

    async def _fake_reconnect(reason: str, *, lock_run: bool = True) -> None:
        reconnect_calls.append({"reason": reason, "lock_run": lock_run})

    with (
        patch("agents.runtime._invoke_sdk", new=AsyncMock(return_value="ok")),
        patch("agents.sdk_pool.is_live_persistent_path_enabled", return_value=True),
        patch("agents.sdk_pool._reconnect_live", side_effect=_fake_reconnect),
    ):
        from agents.runtime import run_user_turn_blocks
        result = await run_user_turn_blocks([{"type": "text", "text": "hello"}])

    assert result == "ok"
    assert len(reconnect_calls) == 1
    assert reconnect_calls[0]["lock_run"] is False


@pytest.mark.asyncio
async def test_reconnect_skipped_when_persistent_disabled():
    """_reconnect_live is NOT called if persistent path is disabled."""
    reconnect_calls: list[dict] = []

    async def _fake_reconnect(reason: str, *, lock_run: bool = True) -> None:
        reconnect_calls.append({"reason": reason, "lock_run": lock_run})

    with (
        patch("agents.runtime._invoke_sdk", new=AsyncMock(return_value="ok")),
        patch("agents.sdk_pool.is_live_persistent_path_enabled", return_value=False),
        patch("agents.sdk_pool._reconnect_live", side_effect=_fake_reconnect),
    ):
        from agents.runtime import run_user_turn_blocks
        await run_user_turn_blocks([{"type": "text", "text": "hello"}])

    assert reconnect_calls == []


@pytest.mark.asyncio
async def test_reconnect_failure_is_non_fatal():
    """If _reconnect_live raises, the turn result is still returned (non-fatal)."""
    async def _fail_reconnect(reason: str, *, lock_run: bool = True) -> None:
        raise RuntimeError("reconnect exploded")

    with (
        patch("agents.runtime._invoke_sdk", new=AsyncMock(return_value="result text")),
        patch("agents.sdk_pool.is_live_persistent_path_enabled", return_value=True),
        patch("agents.sdk_pool._reconnect_live", side_effect=_fail_reconnect),
    ):
        from agents.runtime import run_user_turn_blocks
        result = await run_user_turn_blocks([{"type": "text", "text": "hello"}])

    assert result == "result text"
