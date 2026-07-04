"""Tests for scripts/run_flip_eval.py — exit codes and env guard."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from scripts import run_flip_eval as cli


@pytest.mark.asyncio
async def test_amain_exits_2_without_oauth_token(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert await cli.amain() == 2


@pytest.mark.asyncio
async def test_amain_exit_codes_follow_gate(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "x")
    passing = {"run_id": 1, "bank_version": "v1", "items": [],
               "regressive_rate": 0.0, "anchor_flips": 0, "n_judged": 9}
    failing = {**passing, "anchor_flips": 1}
    # amain() lazily does `from storage import db` and reads the trend view;
    # patch it at source so the test never touches the live production DB.
    with patch.object(cli, "run_flip_eval", new=AsyncMock(return_value=passing)), \
            patch("storage.db.flip_eval_recent_runs", return_value=[]):
        assert await cli.amain() == 0
    with patch.object(cli, "run_flip_eval", new=AsyncMock(return_value=failing)), \
            patch("storage.db.flip_eval_recent_runs", return_value=[]):
        assert await cli.amain() == 1
