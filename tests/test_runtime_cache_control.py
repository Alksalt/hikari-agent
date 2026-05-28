"""Phase B — Item 1: 1h prompt-cache TTL beta flag in ClaudeAgentOptions."""
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


def test_build_options_includes_1h_ttl_beta(monkeypatch):
    monkeypatch.setattr(
        cfg, "get",
        lambda k, d=None: True if k == "runtime.cache_ttl_1h_enabled" else d,
    )
    opts = runtime._build_options(resume=None)
    assert "extended-cache-ttl-2025-04-11" in (opts.betas or [])


def test_build_options_disables_beta_when_flag_off(monkeypatch):
    monkeypatch.setattr(
        cfg, "get",
        lambda k, d=None: False if k == "runtime.cache_ttl_1h_enabled" else d,
    )
    opts = runtime._build_options(resume=None)
    assert "extended-cache-ttl-2025-04-11" not in (opts.betas or [])
