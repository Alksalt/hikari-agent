"""Phase 7 — persona-drift telemetry tests: DB helpers, sampling gate,
judge_outbound parsing, daily-cap enforcement."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agents import config
from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    config.reload()
    yield


# ---------- db helpers ----------

def test_drift_record_round_trip():
    rid = db.drift_record(
        text_snippet="ugh fine.",
        score=0.85,
        class_label="hikari",
        payload='score: 0.85\nclass: hikari\nreason: dry',
    )
    assert rid > 0
    avg = db.drift_recent_avg(window_days=1)
    assert avg == 0.85
    below = db.drift_recent_below_threshold(threshold=0.5, window_days=1)
    assert below == 0


def test_drift_recent_avg_returns_none_on_empty():
    assert db.drift_recent_avg(window_days=7) is None


def test_drift_below_threshold_counts_correctly():
    db.drift_record(text_snippet="a", score=0.2, class_label="drifting")
    db.drift_record(text_snippet="b", score=0.4, class_label="drifting")
    db.drift_record(text_snippet="c", score=0.8, class_label="hikari")
    below = db.drift_recent_below_threshold(threshold=0.5, window_days=1)
    assert below == 2


def test_drift_count_today():
    db.drift_record(text_snippet="x", score=0.5, class_label="unclear")
    db.drift_record(text_snippet="y", score=0.6, class_label="unclear")
    assert db.drift_count_today() == 2


def test_drift_score_clamped():
    rid = db.drift_record(text_snippet="x", score=1.5, class_label="hikari")
    with db._conn() as c:
        row = c.execute(
            "SELECT score FROM persona_drift_scores WHERE id = ?", (rid,)
        ).fetchone()
    assert row["score"] == 1.0
    rid2 = db.drift_record(text_snippet="y", score=-0.3, class_label="drifting")
    with db._conn() as c:
        row = c.execute(
            "SELECT score FROM persona_drift_scores WHERE id = ?", (rid2,)
        ).fetchone()
    assert row["score"] == 0.0


def test_drift_prune_removes_old_samples():
    old_iso = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    with db._conn() as c:
        c.execute(
            "INSERT INTO persona_drift_scores "
            "(text_snippet, score, class_label, sampled_at) "
            "VALUES ('old', 0.5, 'unclear', ?)",
            (old_iso,),
        )
    db.drift_record(text_snippet="fresh", score=0.5, class_label="unclear")
    deleted = db.prune_drift_older_than_days(30)
    assert deleted == 1


# ---------- sampling gate ----------

def test_should_sample_disabled_returns_false(monkeypatch, tmp_path):
    cfg_text = "drift_telemetry:\n  enabled: false\n  rubric: 'x'\n"
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()
    from agents import drift_judge
    assert not drift_judge.should_sample(100)


def test_should_sample_blocked_by_daily_cap(monkeypatch, tmp_path):
    cfg_text = (
        "drift_telemetry:\n"
        "  enabled: true\n"
        "  probability_per_outbound: 1.0\n"
        "  cooldown_min_messages: 0\n"
        "  max_calls_per_day: 2\n"
        "  rubric: 'rubric text'\n"
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()
    db.drift_record(text_snippet="a", score=0.5, class_label="unclear")
    db.drift_record(text_snippet="b", score=0.5, class_label="unclear")
    from agents import drift_judge
    assert not drift_judge.should_sample(100)


def test_should_sample_respects_cooldown(monkeypatch, tmp_path):
    cfg_text = (
        "drift_telemetry:\n"
        "  enabled: true\n"
        "  probability_per_outbound: 1.0\n"
        "  cooldown_min_messages: 5\n"
        "  max_calls_per_day: 100\n"
        "  rubric: 'rubric text'\n"
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()
    from agents import drift_judge
    assert drift_judge.should_sample(1)
    db.runtime_set("drift_last_sampled_at_counter", 1)
    assert not drift_judge.should_sample(2)
    assert not drift_judge.should_sample(5)
    assert drift_judge.should_sample(6)


def test_should_sample_no_rubric_returns_false(monkeypatch, tmp_path):
    cfg_text = "drift_telemetry:\n  enabled: true\n  rubric: ''\n"
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()
    from agents import drift_judge
    assert not drift_judge.should_sample(100)


# ---------- maybe_judge_and_log ----------

@pytest.mark.asyncio
async def test_maybe_judge_no_sample_no_db_write(monkeypatch, tmp_path):
    cfg_text = "drift_telemetry:\n  enabled: false\n  rubric: 'x'\n"
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()
    from agents import drift_judge
    await drift_judge.maybe_judge_and_log("anything", outbound_counter=1)
    assert db.drift_count_today() == 0


@pytest.mark.asyncio
async def test_maybe_judge_writes_when_sampled(monkeypatch, tmp_path):
    cfg_text = (
        "drift_telemetry:\n"
        "  enabled: true\n"
        "  probability_per_outbound: 1.0\n"
        "  cooldown_min_messages: 0\n"
        "  max_calls_per_day: 100\n"
        "  rubric: 'rubric text'\n"
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()

    from agents import drift_judge

    async def fake_judge(text):
        return {
            "score": 0.42, "class": "drifting",
            "reason": "leaked an 'I can help'",
            "raw": "score: 0.42\nclass: drifting",
        }
    monkeypatch.setattr(drift_judge, "judge_outbound", fake_judge)
    await drift_judge.maybe_judge_and_log("hello", outbound_counter=10)
    assert db.drift_count_today() == 1
    avg = db.drift_recent_avg(window_days=1)
    assert avg == 0.42


@pytest.mark.asyncio
async def test_maybe_judge_silent_on_judge_failure(monkeypatch, tmp_path):
    cfg_text = (
        "drift_telemetry:\n"
        "  enabled: true\n"
        "  probability_per_outbound: 1.0\n"
        "  cooldown_min_messages: 0\n"
        "  max_calls_per_day: 100\n"
        "  rubric: 'rubric text'\n"
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()

    from agents import drift_judge

    async def fake_judge(text):
        return None
    monkeypatch.setattr(drift_judge, "judge_outbound", fake_judge)
    await drift_judge.maybe_judge_and_log("hello", outbound_counter=10)
    assert db.drift_count_today() == 0
    # Counter advances even on failure (prevents retry loops).
    assert db.runtime_get_int("drift_last_sampled_at_counter", 0) == 10
