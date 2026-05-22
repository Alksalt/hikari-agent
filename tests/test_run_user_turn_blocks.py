"""Tests for run_user_turn_blocks — verifies it passes blocks (not a string) to _invoke_sdk."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import agents.runtime as runtime_mod


@pytest.mark.asyncio
async def test_run_user_turn_blocks_passes_blocks_to_sdk(monkeypatch):
    captured = {}

    async def fake_invoke_sdk(prompt, *, resume, log_session_id, max_turns,
                               max_budget_usd, retry_on_process_error, **kw):
        captured["prompt"] = prompt
        return "ok"

    monkeypatch.setattr(runtime_mod, "_invoke_sdk", fake_invoke_sdk)
    # Patch the lock so we don't need a real event loop lock context
    monkeypatch.setattr(runtime_mod, "_RUN_LOCK", asyncio.Lock())

    # Patch db.get_session_id to avoid hitting real DB
    with patch("agents.runtime.db") as mock_db:
        mock_db.get_session_id.return_value = None

        blocks = [{"type": "text", "text": "hi"}]
        result = await runtime_mod.run_user_turn_blocks(blocks)

    assert result == "ok"
    assert isinstance(captured["prompt"], list), (
        "run_user_turn_blocks must pass a list to _invoke_sdk, not a string"
    )
    assert captured["prompt"] == blocks
