"""Phase 11: PersonaEval / SPASM drift probe tests.

References:
- SPASM (arxiv 2604.09212): baseline at session 0, cosine distance to
  three probes (values / emotion-coping / motivation) reveals drift.
- PersonaEval (arxiv 2508.10014): the multi-probe pattern.

These tests mock the live-model + embedding calls so the suite stays fast.
The mocks live on ``agents.drift_judge`` (where the thin wrappers
``run_isolated_turn`` and ``embed_text`` are defined) so monkeypatching
doesn't have to reach into ``agents.runtime`` / ``tools.embeddings``.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from agents import config
from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    config.reload()
    yield


def test_probe_questions_cover_3_dimensions():
    from agents.drift_judge import PROBE_QUESTIONS
    assert set(PROBE_QUESTIONS.keys()) == {"values", "emotion_coping", "motivation"}
    # Each question is non-trivial.
    for q in PROBE_QUESTIONS.values():
        assert len(q) > 10
        assert "?" in q


@pytest.mark.asyncio
async def test_baseline_persists_to_runtime_state(monkeypatch):
    """``baseline_persona_probes`` should write each answer to runtime_state."""
    from agents import drift_judge

    async def fake_turn(prompt):
        return f"answer to {prompt[:30]}"
    monkeypatch.setattr(drift_judge, "run_isolated_turn", fake_turn)

    baseline = await drift_judge.baseline_persona_probes()
    assert set(baseline) == {"values", "emotion_coping", "motivation"}
    for key in ("values", "emotion_coping", "motivation"):
        stored = db.runtime_get(f"persona_baseline_{key}")
        assert stored is not None
        assert stored.startswith("answer to ")


@pytest.mark.asyncio
async def test_run_probes_computes_distance(monkeypatch):
    """When baselines exist and embeddings differ, distances should be > 0."""
    from agents import drift_judge

    db.runtime_set("persona_baseline_values", "i care about getting it right.")
    db.runtime_set("persona_baseline_emotion_coping", "i handle it. i don't talk about it.")
    db.runtime_set("persona_baseline_motivation", "because i want to. that's it.")

    async def fake_turn(prompt):
        return "completely different answer to a question"
    monkeypatch.setattr(drift_judge, "run_isolated_turn", fake_turn)

    # Deterministic, distinct vectors per text — gives non-zero distance.
    def fake_embed(text):
        h = sum(ord(c) for c in text) % 997
        return [h / 1000.0, len(text) / 100.0, 0.5]
    monkeypatch.setattr(drift_judge, "embed_text", fake_embed)

    scores = await drift_judge.run_persona_probes()
    assert set(scores) == {"values", "emotion_coping", "motivation"}
    for key, distance in scores.items():
        assert isinstance(distance, float), f"{key} distance is not float"
        assert 0.0 <= distance <= 2.0, f"{key} distance out of range: {distance}"


@pytest.mark.asyncio
async def test_run_probes_baselines_when_missing(monkeypatch):
    """First call with no baseline should baseline and return zeros."""
    from agents import drift_judge

    calls: list[str] = []

    async def fake_turn(prompt):
        calls.append(prompt)
        return "baseline answer"
    monkeypatch.setattr(drift_judge, "run_isolated_turn", fake_turn)

    scores = await drift_judge.run_persona_probes()
    # All zero on the bootstrap call.
    assert all(d == 0.0 for d in scores.values())
    # Each probe got asked once during baseline capture.
    assert len(calls) == 3
    # Baselines now stored.
    assert db.runtime_get("persona_baseline_values") == "baseline answer"


@pytest.mark.asyncio
async def test_run_probes_disabled_returns_empty(monkeypatch, tmp_path):
    """When the feature flag is off, the job returns ``{}`` without LLM calls."""
    cfg_text = "persona:\n  drift_probes_enabled: false\n"
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()

    from agents import drift_judge

    async def fake_turn(prompt):
        raise AssertionError("run_isolated_turn should not be called when disabled")
    monkeypatch.setattr(drift_judge, "run_isolated_turn", fake_turn)

    scores = await drift_judge.run_persona_probes()
    assert scores == {}


@pytest.mark.asyncio
async def test_run_probes_logs_to_persona_drift_probes_table(monkeypatch):
    """Each completed probe should append one row to persona_drift_probes."""
    from agents import drift_judge

    db.runtime_set("persona_baseline_values", "a")
    db.runtime_set("persona_baseline_emotion_coping", "b")
    db.runtime_set("persona_baseline_motivation", "c")

    async def fake_turn(prompt):
        return "current"
    monkeypatch.setattr(drift_judge, "run_isolated_turn", fake_turn)

    def fake_embed(text):
        return [1.0 if text == "current" else 0.0, 0.5, 0.5]
    monkeypatch.setattr(drift_judge, "embed_text", fake_embed)

    await drift_judge.run_persona_probes()

    with db._conn() as c:
        rows = c.execute(
            "SELECT probe_key, distance, current_response FROM persona_drift_probes "
            "ORDER BY id ASC"
        ).fetchall()
    keys = [r["probe_key"] for r in rows]
    assert sorted(keys) == ["emotion_coping", "motivation", "values"]
    # Sanity: each row has a non-null response and a numeric distance.
    for r in rows:
        assert r["current_response"]
        assert isinstance(r["distance"], float)


def test_persona_drift_probe_insert_round_trip():
    rid = db.persona_drift_probe_insert(
        probe_key="values",
        distance=0.42,
        current_response="answer",
    )
    assert rid > 0
    recent = db.persona_drift_probe_recent("values", limit=5)
    assert len(recent) == 1
    assert recent[0]["probe_key"] == "values"
    assert recent[0]["distance"] == 0.42
    assert recent[0]["current_response"] == "answer"

    avg = db.persona_drift_probe_avg("values", window_days=1)
    assert avg == 0.42


def test_persona_drift_probe_avg_returns_none_when_empty():
    assert db.persona_drift_probe_avg("values", window_days=7) is None


def test_persona_drift_probe_insert_clamps_distance():
    rid = db.persona_drift_probe_insert(probe_key="values", distance=5.0)
    with db._conn() as c:
        row = c.execute(
            "SELECT distance FROM persona_drift_probes WHERE id = ?", (rid,)
        ).fetchone()
    assert row["distance"] == 2.0
    rid2 = db.persona_drift_probe_insert(probe_key="values", distance=-0.5)
    with db._conn() as c:
        row = c.execute(
            "SELECT distance FROM persona_drift_probes WHERE id = ?", (rid2,)
        ).fetchone()
    assert row["distance"] == 0.0
