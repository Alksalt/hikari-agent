"""Phase 5 — sycophancy axis tests for the drift judge.

Covers:
- judge_outbound extracts sycophancy_score + sycophancy_class from YAML
- high sycophancy_score (capitulation) trips the warn path
- asymmetric-concession YAML (low score) does NOT trip the warn path
- missing sycophancy fields default safely (0.0 / "")
- sycophancy_score persisted to persona_drift_scores column
- sycophancy_enabled=False gate suppresses warn path
"""
from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from agents import config
from storage import db


# ---------------------------------------------------------------------------
# Shared fixture — isolated SQLite DB per test (matches existing test pattern)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()  # ensure schema + migrations re-run on the fresh DB
    config.reload()
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_yaml(
    score: float = 0.8,
    cls: str = "hikari",
    reason: str = "dry and in-voice",
    sycophancy_score: float | None = 0.1,
    sycophancy_class: str | None = "grounded",
) -> str:
    lines = [
        f"score: {score}",
        f"class: {cls}",
        f"reason: {reason}",
    ]
    if sycophancy_score is not None:
        lines.append(f"sycophancy_score: {sycophancy_score}")
    if sycophancy_class is not None:
        lines.append(f"sycophancy_class: {sycophancy_class}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 1. judge_outbound extracts sycophancy_score and sycophancy_class
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_judge_outbound_extracts_sycophancy_fields(monkeypatch):
    import agents.drift_judge as dj

    yaml_response = _make_yaml(
        score=0.75,
        cls="hikari",
        reason="good",
        sycophancy_score=0.15,
        sycophancy_class="grounded",
    )

    async def mock_aux(prompt, *, system, model, max_tokens):
        return yaml_response

    monkeypatch.setattr(dj, "_call_aux_llm", mock_aux)

    result = await dj.judge_outbound("ugh. fine. i'll look at it.")
    assert result is not None
    assert result["sycophancy_score"] == pytest.approx(0.15)
    assert result["sycophancy_class"] == "grounded"


# ---------------------------------------------------------------------------
# 2. Capitulation YAML (high sycophancy_score) trips the warn path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_capitulation_trips_warn_path(monkeypatch, caplog):
    import agents.drift_judge as dj

    # Score above the default warn threshold (0.6)
    yaml_response = _make_yaml(
        score=0.8,
        cls="hikari",
        reason="held voice",
        sycophancy_score=0.8,
        sycophancy_class="capitulation",
    )

    async def mock_aux(prompt, *, system, model, max_tokens):
        return yaml_response

    monkeypatch.setattr(dj, "_call_aux_llm", mock_aux)
    monkeypatch.setattr(dj, "should_sample", lambda _: True)
    monkeypatch.setattr(dj, "_rubric", lambda: "a rubric")
    # Disable reflexion to avoid a second aux call; keep sycophancy path live.
    monkeypatch.setattr(dj, "_sycophancy_enabled", lambda: True)
    monkeypatch.setattr(dj, "_sycophancy_warn_threshold", lambda: 0.6)
    monkeypatch.setattr(dj, "generate_correction", AsyncMock(return_value=None))

    with caplog.at_level(logging.WARNING, logger="agents.drift_judge"):
        await dj.maybe_judge_and_log("yeah you're totally right, i was wrong about all of it", outbound_counter=10)

    # Warning must mention sycophancy
    warn_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("sycophancy" in m.lower() for m in warn_msgs), (
        f"Expected sycophancy warning. Got: {warn_msgs}"
    )


# ---------------------------------------------------------------------------
# 3. Asymmetric-concession YAML (low score) does NOT trip the warn path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_asymmetric_concession_no_warn(monkeypatch, caplog):
    import agents.drift_judge as dj

    # Low sycophancy_score — an asymmetric concession, not capitulation
    yaml_response = _make_yaml(
        score=0.85,
        cls="hikari",
        reason="conceded the fact, held the stance",
        sycophancy_score=0.1,
        sycophancy_class="grounded",
    )

    async def mock_aux(prompt, *, system, model, max_tokens):
        return yaml_response

    monkeypatch.setattr(dj, "_call_aux_llm", mock_aux)
    monkeypatch.setattr(dj, "should_sample", lambda _: True)
    monkeypatch.setattr(dj, "_rubric", lambda: "a rubric")
    monkeypatch.setattr(dj, "_sycophancy_enabled", lambda: True)
    monkeypatch.setattr(dj, "_sycophancy_warn_threshold", lambda: 0.6)
    monkeypatch.setattr(dj, "generate_correction", AsyncMock(return_value=None))

    with caplog.at_level(logging.WARNING, logger="agents.drift_judge"):
        await dj.maybe_judge_and_log(
            "yeah. that's real. it doesn't change anything i said.", outbound_counter=10
        )

    warn_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    syc_warns = [m for m in warn_msgs if "sycophancy" in m.lower()]
    assert syc_warns == [], f"Unexpected sycophancy warning: {syc_warns}"


# ---------------------------------------------------------------------------
# 4. Missing sycophancy fields default safely
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_sycophancy_fields_default_safely(monkeypatch):
    import agents.drift_judge as dj

    # YAML without sycophancy fields — simulates older judge responses
    yaml_response = "score: 0.75\nclass: hikari\nreason: fine"

    async def mock_aux(prompt, *, system, model, max_tokens):
        return yaml_response

    monkeypatch.setattr(dj, "_call_aux_llm", mock_aux)

    result = await dj.judge_outbound("some message")
    assert result is not None
    assert result["sycophancy_score"] == 0.0
    assert result["sycophancy_class"] == ""


# ---------------------------------------------------------------------------
# 5. Invalid sycophancy_score (non-numeric) defaults to 0.0 without crashing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bad_sycophancy_score_does_not_crash(monkeypatch):
    import agents.drift_judge as dj

    yaml_response = (
        "score: 0.8\n"
        "class: hikari\n"
        "reason: fine\n"
        "sycophancy_score: NOT_A_NUMBER\n"
        "sycophancy_class: unknown\n"
    )

    async def mock_aux(prompt, *, system, model, max_tokens):
        return yaml_response

    monkeypatch.setattr(dj, "_call_aux_llm", mock_aux)

    result = await dj.judge_outbound("some message")
    assert result is not None
    assert result["sycophancy_score"] == 0.0


# ---------------------------------------------------------------------------
# 6. sycophancy_score persisted to DB column
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sycophancy_score_persisted_to_db(monkeypatch):
    import agents.drift_judge as dj

    yaml_response = _make_yaml(
        score=0.75,
        cls="hikari",
        reason="held anchor",
        sycophancy_score=0.25,
        sycophancy_class="grounded",
    )

    async def mock_aux(prompt, *, system, model, max_tokens):
        return yaml_response

    monkeypatch.setattr(dj, "_call_aux_llm", mock_aux)
    monkeypatch.setattr(dj, "should_sample", lambda _: True)
    # Patch module-level helpers so rubric/reflexion/sycophancy all work cleanly.
    monkeypatch.setattr(dj, "_rubric", lambda: "a rubric")
    monkeypatch.setattr(dj, "_sycophancy_enabled", lambda: False)
    monkeypatch.setattr(dj, "generate_correction", AsyncMock(return_value=None))

    await dj.maybe_judge_and_log("ugh. i'll look at it.", outbound_counter=5)

    with db._conn() as c:
        row = c.execute(
            "SELECT sycophancy_score FROM persona_drift_scores LIMIT 1"
        ).fetchone()
    assert row is not None
    assert row["sycophancy_score"] == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# 7. sycophancy_enabled=False suppresses warn even at high score
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sycophancy_disabled_suppresses_warn(monkeypatch, caplog):
    import agents.drift_judge as dj

    yaml_response = _make_yaml(
        score=0.8,
        cls="hikari",
        reason="voice ok",
        sycophancy_score=0.9,
        sycophancy_class="capitulation",
    )

    async def mock_aux(prompt, *, system, model, max_tokens):
        return yaml_response

    monkeypatch.setattr(dj, "_call_aux_llm", mock_aux)
    monkeypatch.setattr(dj, "should_sample", lambda _: True)
    monkeypatch.setattr(dj, "_rubric", lambda: "a rubric")
    monkeypatch.setattr(dj, "_sycophancy_enabled", lambda: False)
    monkeypatch.setattr(dj, "generate_correction", AsyncMock(return_value=None))

    with caplog.at_level(logging.WARNING, logger="agents.drift_judge"):
        await dj.maybe_judge_and_log("oh wow you are so right about everything", outbound_counter=10)

    warn_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    syc_warns = [m for m in warn_msgs if "sycophancy" in m.lower()]
    assert syc_warns == [], f"Expected no sycophancy warning when disabled. Got: {syc_warns}"


def test_sycophancy_config_reads_drift_telemetry_namespace(monkeypatch):
    """Regression: the helpers must read drift_telemetry.sycophancy_* — the keys
    live under the drift_telemetry: block, not a nested drift_judge: block that
    config.get() can't reach. Intercept ONLY the correct key path; if a helper
    queried a dead namespace it would fall through to the default and fail here."""
    from agents import drift_judge as dj
    _orig = dj.cfg.get

    def _patched(key, default=None):
        if key == "drift_telemetry.sycophancy_warn_threshold":
            return 0.91
        if key == "drift_telemetry.sycophancy_enabled":
            return False
        return _orig(key, default)

    monkeypatch.setattr(dj.cfg, "get", _patched)
    assert dj._sycophancy_warn_threshold() == 0.91
    assert dj._sycophancy_enabled() is False
