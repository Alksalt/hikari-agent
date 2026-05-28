"""Phase C — main-chat cost rollup + cockpit status display."""
from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from storage import db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    # Bootstrap schema (creates llm_costs table among others).
    db.upsert_core_block("_boot", "_boot")
    yield
    db._reset_schema_sentinel()


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", MagicMock(side_effect=OSError("blocked")))


def _insert_cost(model: str, cost_usd: float, hours_ago: float = 0.0):
    """Insert a row into llm_costs with ts set hours_ago in the past."""
    ts = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    with db._conn() as c:
        c.execute(
            "INSERT INTO llm_costs (ts, turn_id, model, path, "
            "input_tokens, output_tokens, "
            "cache_read_input_tokens, cache_creation_input_tokens, cost_usd) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, None, model, "persistent", 100, 50, 0, 0, cost_usd),
        )


# ---------------------------------------------------------------------------
# 1. llm_costs_rollup — 24h window filter
# ---------------------------------------------------------------------------

def test_llm_costs_rollup_24h_window():
    _insert_cost("claude-sonnet-4-6", 0.10, hours_ago=2)   # inside 24h
    _insert_cost("claude-sonnet-4-6", 0.20, hours_ago=12)  # inside 24h
    _insert_cost("claude-sonnet-4-6", 1.00, hours_ago=25)  # outside 24h

    result = db.llm_costs_rollup(window_hours=24)

    assert result["n_rows"] == 2
    assert result["total_cost_usd"] == pytest.approx(0.30, rel=0.001)


# ---------------------------------------------------------------------------
# 2. llm_costs_rollup — per-model breakdown
# ---------------------------------------------------------------------------

def test_llm_costs_rollup_by_model_breakdown():
    _insert_cost("claude-sonnet-4-6", 0.50, hours_ago=1)
    _insert_cost("claude-sonnet-4-5", 0.20, hours_ago=2)
    _insert_cost("claude-sonnet-4-6", 0.10, hours_ago=3)

    result = db.llm_costs_rollup(window_hours=24)

    assert result["n_rows"] == 3
    assert result["total_cost_usd"] == pytest.approx(0.80, rel=0.001)
    assert result["by_model"]["claude-sonnet-4-6"] == pytest.approx(0.60, rel=0.001)
    assert result["by_model"]["claude-sonnet-4-5"] == pytest.approx(0.20, rel=0.001)
    # Sorted descending by cost
    models_ordered = list(result["by_model"].keys())
    assert models_ordered[0] == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# 3. /cockpit status — shows new chat cost lines
# ---------------------------------------------------------------------------

def _make_mock_app():
    """Return a minimal mock app that satisfies format_status's scheduler probe."""
    app = MagicMock()
    app.bot_data = {}
    return app


@pytest.mark.asyncio
async def test_cockpit_status_shows_main_chat_cost(monkeypatch):
    _insert_cost("claude-sonnet-4-6", 0.05, hours_ago=1)

    from agents import cockpit as ck
    from agents import config as cfg

    monkeypatch.setattr(cfg, "get", lambda k, d=None: (
        200.0 if k == "runtime.agent_sdk_monthly_credit_usd" else d
    ))

    with (
        patch("tools.budget.daily_cap", return_value=1.00),
        patch.object(db, "runtime_get", return_value="0.0"),
    ):
        text = await ck.format_status(_make_mock_app())

    assert "chat cost" in text
    assert "24h:" in text
    assert "30d:" in text
    assert "turns" in text


# ---------------------------------------------------------------------------
# 4. /cockpit status — 80% alert fires when 30d cost > threshold
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cockpit_status_alerts_at_80pct(monkeypatch):
    # Seed 30d cost > $160 (80% of $200).
    _insert_cost("claude-sonnet-4-6", 80.00, hours_ago=24 * 5)   # 5 days ago
    _insert_cost("claude-sonnet-4-6", 85.00, hours_ago=24 * 10)  # 10 days ago

    from agents import cockpit as ck
    from agents import config as cfg

    monkeypatch.setattr(cfg, "get", lambda k, d=None: (
        200.0 if k == "runtime.agent_sdk_monthly_credit_usd" else d
    ))

    with (
        patch("tools.budget.daily_cap", return_value=1.00),
        patch.object(db, "runtime_get", return_value="0.0"),
    ):
        text = await ck.format_status(_make_mock_app())

    assert "80%" in text
    assert "200" in text


@pytest.mark.asyncio
async def test_cockpit_status_no_alert_below_80pct(monkeypatch):
    _insert_cost("claude-sonnet-4-6", 1.00, hours_ago=1)

    from agents import cockpit as ck
    from agents import config as cfg

    monkeypatch.setattr(cfg, "get", lambda k, d=None: (
        200.0 if k == "runtime.agent_sdk_monthly_credit_usd" else d
    ))

    with (
        patch("tools.budget.daily_cap", return_value=1.00),
        patch.object(db, "runtime_get", return_value="0.0"),
    ):
        text = await ck.format_status(_make_mock_app())

    assert "80%" not in text
