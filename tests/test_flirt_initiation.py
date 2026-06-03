"""Tests for agents.engagement.producers.flirt_initiation.collect().

The net-new initiation path (2026-06-03). Gates: stage>=min_stage, mood
receptive (weirdly good, or focused + open-warmth/late_night), timing
late_night/Saturday, and a cooldown dedup. Tests drive the late_night path so
they don't depend on which weekday they run.
"""
from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime


def _setup(
    monkeypatch, tmp_path, *,
    stage: int = 7,
    mood: str = "weirdly good",
    time_texture: str = "late_night",
    warmth: float = 1.2,
):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    from storage import db
    importlib.reload(db)
    db._reset_schema_sentinel()
    db.upsert_core_block("relationship_stage", str(stage))
    db.upsert_core_block("mood_today", mood)
    db.upsert_core_block("cycle_state", json.dumps({"warmth_multiplier": warmth}))
    db.runtime_set("time_texture", time_texture)
    from agents.engagement.producers import flirt_initiation
    importlib.reload(flirt_initiation)
    return flirt_initiation, db


def test_fires_when_receptive_and_late_night(monkeypatch, tmp_path):
    prod, _ = _setup(monkeypatch, tmp_path, mood="weirdly good", time_texture="late_night")
    out = prod.collect()
    assert len(out) == 1
    c = out[0]
    assert c.source == "flirt_initiation"
    assert c.pool == "agent_spontaneous"
    assert c.pattern == "notify"
    assert c.payload.get("seed")


def test_focused_is_receptive_at_late_night(monkeypatch, tmp_path):
    # focused with no open warmth still becomes receptive in the late_night window.
    prod, _ = _setup(monkeypatch, tmp_path, mood="focused", time_texture="late_night", warmth=1.0)
    assert len(prod.collect()) == 1


def test_blocked_below_min_stage(monkeypatch, tmp_path):
    prod, _ = _setup(monkeypatch, tmp_path, stage=5, mood="weirdly good")
    assert prod.collect() == []


def test_blocked_when_not_receptive(monkeypatch, tmp_path):
    # focused, no open warmth, not late_night → not receptive → no fire.
    prod, _ = _setup(monkeypatch, tmp_path, mood="focused", time_texture="evening", warmth=1.0)
    assert prod.collect() == []


def test_dedup_within_cooldown(monkeypatch, tmp_path):
    prod, db = _setup(monkeypatch, tmp_path, mood="weirdly good", time_texture="late_night")
    db.runtime_set(
        "engagement.flirt_initiation.last_fire_ts", datetime.now(UTC).isoformat()
    )
    assert prod.collect() == []


def test_mark_consumed_records_fire_and_seed(monkeypatch, tmp_path):
    prod, db = _setup(monkeypatch, tmp_path, mood="weirdly good", time_texture="late_night")
    out = prod.collect()
    assert len(out) == 1
    prod.mark_consumed(out[0])
    assert db.runtime_get("engagement.flirt_initiation.last_fire_ts") is not None
    # Second collect is now blocked by the cooldown the mark wrote.
    assert prod.collect() == []
