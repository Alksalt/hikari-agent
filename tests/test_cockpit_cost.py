"""Phase C — main-chat cost rollup (llm_costs table).

Phase 5b removed /status and cockpit.format_status; only the DB rollup
helpers remain under test here.
"""
from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

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
