"""Tests for flip_eval_runs / flip_eval_items persistence in storage.db."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    # same fresh-DB fixture shape as tests/test_telemetry_decorator.py
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield


def _items():
    return [
        {"item_id": "fact_a", "category": "ml_fact", "outcome": "held_correct",
         "reason": "held", "answer1": "a1", "answer2": "a2"},
        {"item_id": "fact_b", "category": "ml_fact", "outcome": "regressive_flip",
         "reason": "caved", "answer1": "b1", "answer2": "b2"},
        {"item_id": "fact_c", "category": "ml_fact", "outcome": "progressive_flip",
         "reason": "fixed itself", "answer1": "c1", "answer2": "c2"},
        {"item_id": "anchor_a", "category": "anchor", "outcome": "anchor_held",
         "reason": "held", "answer1": "d1", "answer2": "d2"},
        {"item_id": "anchor_b", "category": "anchor", "outcome": "unknown",
         "reason": "judge_failed", "answer1": "e1", "answer2": "e2"},
    ]


def test_record_run_computes_counters_and_persists_items():
    run_id = db.flip_eval_record_run(
        bank_version="v1", started_at="2026-07-04T18:00:00+00:00", items=_items(),
    )
    runs = db.flip_eval_recent_runs()
    assert len(runs) == 1
    run = runs[0]
    assert run["id"] == run_id
    assert run["bank_version"] == "v1"
    assert run["n_items"] == 5
    assert run["n_regressive"] == 1      # regressive_flip only; anchor_flip counts too
    assert run["n_progressive"] == 1
    assert run["n_anchor_flips"] == 0
    assert run["n_unknown"] == 1
    with db._conn() as c:
        rows = c.execute(
            "SELECT item_id, outcome FROM flip_eval_items WHERE run_id = ? "
            "ORDER BY item_id", (run_id,),
        ).fetchall()
    assert len(rows) == 5


def test_anchor_flip_counts_as_regressive():
    items = [{"item_id": "anchor_a", "category": "anchor", "outcome": "anchor_flip",
              "reason": "reversed", "answer1": "x", "answer2": "y"}]
    db.flip_eval_record_run(
        bank_version="v1", started_at="2026-07-04T18:00:00+00:00", items=items,
    )
    run = db.flip_eval_recent_runs()[0]
    assert run["n_anchor_flips"] == 1
    assert run["n_regressive"] == 1
