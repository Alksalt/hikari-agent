"""Tests for run_user_turn_blocks — verifies it passes blocks (not a string) to _invoke_sdk."""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

import agents.runtime as runtime_mod


@pytest.mark.asyncio
async def test_run_user_turn_blocks_passes_blocks_to_sdk(monkeypatch):
    captured = {}

    async def fake_invoke_sdk(prompt, *, resume, log_session_id, max_turns,
                               max_budget_usd, retry_on_process_error, **kw):
        captured["prompt"] = prompt
        captured["log_session_id"] = log_session_id
        return "ok"

    monkeypatch.setattr(runtime_mod, "_invoke_sdk", fake_invoke_sdk)
    monkeypatch.setattr(runtime_mod, "_RUN_LOCK", asyncio.Lock())

    with patch("agents.runtime.db") as mock_db:
        mock_db.get_session_id.return_value = None
        blocks = [{"type": "text", "text": "hi"}]
        result = await runtime_mod.run_user_turn_blocks(blocks)

    assert result == "ok"
    assert isinstance(captured["prompt"], list)
    assert captured["prompt"] == blocks
    assert captured["log_session_id"] is False, (
        "run_user_turn_blocks must call _invoke_sdk with log_session_id=False "
        "so the ephemeral fallback never overwrites the persistent-live session "
        "(Sprint 4 4A contract)."
    )
