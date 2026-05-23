"""Decision log: schema, helpers, Brier scoring, resolver, capture tool."""
from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    yield


@pytest.fixture(autouse=True)
def _gate_open(monkeypatch):
    """Ensure the proactive gate never suppresses due to quiet hours or
    silence window in unit tests — those paths are covered by
    test_proactive_global_reservation.py."""
    import agents.proactive_gate as _gate
    monkeypatch.setattr(_gate, "_is_quiet_now", lambda _db=None: False)
    monkeypatch.setattr(_gate, "_silence_active", lambda _db: False)


def test_decision_table_created():
    from storage import db
    db.upsert_core_block("ping", "ping")  # triggers schema bootstrap
    with db._conn() as c:
        cols = {r["name"] for r in
                c.execute("PRAGMA table_info(decisions)").fetchall()}
    for col in ("statement", "predicted_p", "resolve_by",
                "outcome", "resolved_at", "asked_at"):
        assert col in cols


def test_decision_insert_and_resolve_round_trip():
    from storage import db
    did = db.decision_insert("ship by friday", 0.8, "2026-05-25", "feel ok")
    assert did > 0
    db.decision_resolve(did, 1)
    score = db.decision_brier_score(window_days=365)
    assert score["n"] == 1
    assert score["brier"] == pytest.approx(0.04)  # (0.8 - 1)^2 = 0.04


def test_decisions_unresolved_due_filters_future_dates():
    from storage import db
    db.decision_insert("past", 0.5, "2026-01-01")
    db.decision_insert("future", 0.5, "2099-01-01")
    rows = db.decisions_unresolved_due(limit=10)
    statements = [r["statement"] for r in rows]
    assert "past" in statements
    assert "future" not in statements


def test_decisions_unresolved_overdue_count():
    from storage import db
    db.decision_insert("past1", 0.5, "2026-01-01")
    db.decision_insert("past2", 0.5, "2026-01-02")
    db.decision_insert("future", 0.5, "2099-01-01")
    assert db.decisions_unresolved_overdue_count() == 2


def test_resolve_validates_outcome():
    from storage import db
    did = db.decision_insert("x", 0.5, "2026-01-01")
    with pytest.raises(ValueError):
        db.decision_resolve(did, 2)


def test_brier_score_empty_window():
    from storage import db
    score = db.decision_brier_score(window_days=30)
    assert score == {"n": 0}


def test_brier_score_clamps_predicted_p():
    """Out-of-range probabilities are clamped at insert time."""
    from storage import db
    db.decision_insert("over", 1.5, "2026-01-01")
    db.decision_insert("under", -0.2, "2026-01-01")
    rows = db.decisions_unresolved_due(limit=10)
    for r in rows:
        assert 0.0 <= r["predicted_p"] <= 1.0


@pytest.mark.asyncio
async def test_resolver_asks_about_overdue_decisions():
    from agents import decision_log
    from storage import db
    db.decision_insert("ship by yesterday", 0.7, "2026-01-01")
    db.decision_insert("future thing", 0.5, "2099-01-01")

    send = AsyncMock(return_value=("ok", 1, True))
    n = await decision_log.run_decision_resolver(send)
    assert n == 1
    call_text = send.call_args.args[0]
    assert "ship by yesterday" in call_text


@pytest.mark.asyncio
async def test_resolver_marks_asked():
    """After asking, asked_at must be stamped so the next run can skip it
    via the cooldown (if/when implemented)."""
    from agents import decision_log
    from storage import db
    did = db.decision_insert("past", 0.5, "2026-01-01")

    send = AsyncMock(return_value=("ok", 1, True))
    await decision_log.run_decision_resolver(send)

    with db._conn() as c:
        row = c.execute(
            "SELECT asked_at FROM decisions WHERE id = ?", (did,)
        ).fetchone()
    assert row["asked_at"] is not None


@pytest.mark.asyncio
async def test_resolver_does_nothing_when_no_overdue():
    from agents import decision_log
    send = AsyncMock(return_value=("ok", 1, True))
    n = await decision_log.run_decision_resolver(send)
    assert n == 0
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolver_disabled_by_config(monkeypatch):
    from agents import config as cfg
    from agents import decision_log
    from storage import db
    db.decision_insert("past", 0.5, "2026-01-01")

    original_get = cfg.get
    monkeypatch.setattr(cfg, "get",
                        lambda k, d=None: False if k == "decision_log.enabled"
                                          else original_get(k, d))
    send = AsyncMock(return_value=("ok", 1, True))
    n = await decision_log.run_decision_resolver(send)
    assert n == 0
    send.assert_not_awaited()


def test_capture_tool_args_validation():
    """decision_log_capture rejects missing required fields."""
    import asyncio
    from tools.decision_log.capture import decision_log_capture
    # Empty statement → friendly error.
    r = asyncio.run(decision_log_capture.handler(
        {"statement": "", "predicted_p": 0.5, "resolve_by": "2026-12-31"}))
    assert "required" in str(r).lower() or "missing" in str(r).lower()
