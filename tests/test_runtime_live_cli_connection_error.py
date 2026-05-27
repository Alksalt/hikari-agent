"""Persistent live runtime recovers when the cached CLI subprocess is dead."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
from claude_agent_sdk._errors import CLIConnectionError
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


class _DeadClient:
    async def query(self, prompt):
        raise CLIConnectionError("Cannot write to terminated process (exit code: 143)")


class _HealthyClient:
    async def query(self, prompt):
        return None

    async def receive_response(self):
        yield AssistantMessage(content=[TextBlock("back online")], model="fake")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="session-after-retry",
        )


@pytest.mark.asyncio
async def test_persistent_live_reconnects_after_cli_connection_error_without_clearing_session(
    monkeypatch,
):
    """A dead cached Claude CLI process should be replaced, not reused forever."""
    import agents.runtime as runtime
    import agents.sdk_pool as pool

    db.set_session_id("session-before-retry")
    clients = [_DeadClient(), _HealthyClient()]
    reconnect_sessions: list[str | None] = []
    reconnect_calls: list[tuple[str, bool]] = []

    async def fake_get_live_client():
        return clients.pop(0)

    async def fake_reconnect(reason: str, *, lock_run: bool = True):
        reconnect_calls.append((reason, lock_run))
        reconnect_sessions.append(db.get_session_id())

    monkeypatch.setattr(pool, "get_live_client", fake_get_live_client)
    monkeypatch.setattr(pool, "_reconnect_live", fake_reconnect)
    monkeypatch.setattr(pool, "_maybe_schedule_live_recycle", lambda: None)

    result = await runtime._invoke_sdk_persistent_live(
        "ping",
        log_session_id=True,
    )

    assert result == "back online"
    assert reconnect_calls == [("CLIConnectionError on user turn", False)]
    assert reconnect_sessions == ["session-before-retry"]
    assert db.get_session_id() == "session-after-retry"
