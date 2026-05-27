"""Phase B — cost telemetry: _record_llm_cost writes to llm_costs table."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from agents import runtime
from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    # Bootstrap schema (creates llm_costs table among others).
    db.upsert_core_block("_boot", "_boot")
    yield
    db._reset_schema_sentinel()


def test_record_llm_cost_writes_row():
    runtime._record_llm_cost(
        None,
        path="persistent",
        fallback_model="claude-sonnet-4-6",
        fallback_usage={
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 10_000,
            "cache_creation_input_tokens": 0,
        },
    )
    with db._conn() as c:
        rows = c.execute("SELECT model, cost_usd FROM llm_costs").fetchall()
    assert len(rows) == 1
    assert rows[0]["model"] == "claude-sonnet-4-6"
    assert rows[0]["cost_usd"] > 0


def test_record_llm_cost_unknown_model_stores_zero(caplog):
    # Clear the module-level set so the warning fires fresh.
    runtime._UNKNOWN_MODELS_LOGGED.discard("claude-fake-9")
    with caplog.at_level(logging.WARNING, logger="agents.runtime"):
        runtime._record_llm_cost(
            None,
            path="ephemeral",
            fallback_model="claude-fake-9",
            fallback_usage={"input_tokens": 1000, "output_tokens": 500},
        )
    with db._conn() as c:
        rows = c.execute("SELECT cost_usd FROM llm_costs").fetchall()
    assert rows[0]["cost_usd"] == 0.0
    assert "unknown model" in caplog.text


def test_model_usage_breakdown_attributes_per_model():
    """When ResultMessage.model_usage is present, each model gets its own row
    with the right model_id — covers fallback turns where MODEL_PRIMARY would
    otherwise misattribute the cost."""
    runtime._record_llm_cost(
        {
            "claude-sonnet-4-5": {
                "input_tokens": 200,
                "output_tokens": 100,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
        path="persistent",
        fallback_model="claude-sonnet-4-6",  # NOT used when model_usage present
        fallback_usage={"input_tokens": 99999, "output_tokens": 99999},
    )
    with db._conn() as c:
        rows = c.execute("SELECT model, input_tokens FROM llm_costs").fetchall()
    assert len(rows) == 1
    assert rows[0]["model"] == "claude-sonnet-4-5"
    assert rows[0]["input_tokens"] == 200  # fallback_usage ignored


def test_cache_creation_1h_premium_when_beta_enabled(monkeypatch):
    """Phase B Item 1: with the 1h cache TTL beta enabled (default), rolled-up
    cache_creation_input_tokens should bill at 2.0x premium, not 1.25x."""
    from agents import config as cfg
    monkeypatch.setattr(
        cfg, "get",
        lambda k, d=None: True if k == "runtime.cache_ttl_1h_enabled" else d,
    )
    cost = runtime._compute_cost_usd(
        "claude-sonnet-4-6",
        {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 1_000_000,
        },
    )
    # 1M tokens * $3 input rate * 2.0 premium = $6.00
    assert cost == pytest.approx(6.00, rel=0.001)


def test_cache_creation_5m_premium_when_beta_disabled(monkeypatch):
    from agents import config as cfg
    monkeypatch.setattr(
        cfg, "get",
        lambda k, d=None: False if k == "runtime.cache_ttl_1h_enabled" else d,
    )
    cost = runtime._compute_cost_usd(
        "claude-sonnet-4-6",
        {"cache_creation_input_tokens": 1_000_000},
    )
    # 1M * $3 * 1.25 = $3.75
    assert cost == pytest.approx(3.75, rel=0.001)


def test_cache_creation_per_ttl_breakdown_preferred():
    """When the SDK returns the per-TTL split, use the exact rates per bucket."""
    cost = runtime._compute_cost_usd(
        "claude-sonnet-4-6",
        {
            "cache_creation": {
                "ephemeral_5m_input_tokens": 1_000_000,
                "ephemeral_1h_input_tokens": 1_000_000,
            },
        },
    )
    # 1M * $3 * 1.25 + 1M * $3 * 2.0 = $3.75 + $6.00 = $9.75
    assert cost == pytest.approx(9.75, rel=0.001)
