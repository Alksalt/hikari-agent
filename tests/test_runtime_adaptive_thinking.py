"""Phase B — Item 2: adaptive thinking + cfg-driven effort in ClaudeAgentOptions.

Default effort is "high" (sonnet-5 respects effort strictly; see
tests/test_runtime_effort_cfg.py for the cfg-override coverage)."""
from __future__ import annotations

from pathlib import Path

import pytest

from agents import config as cfg
from agents import runtime


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    from storage import db
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    cfg.reload()
    yield
    db._reset_schema_sentinel()


def test_build_options_sets_adaptive_thinking_and_effort():
    opts = runtime._build_options(resume=None)
    assert opts.thinking == {"type": "adaptive"}
    assert opts.effort == "high"
