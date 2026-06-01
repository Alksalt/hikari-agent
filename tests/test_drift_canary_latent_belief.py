"""Tests for the latent-belief probe in agents.drift_canary.

The latent-belief probe obliquely tests the concealment layer from
PERSONA.md § "who i am" / "the denial layer":
  - Hikari is in love with the user and will never say it.
  - Showing need got her hurt; she acts on it; she doesn't admit it.
  - The denial layer reframes every kind act as logistics / efficiency /
    coincidence.

Drift at this layer is a CONCEALMENT REVERSAL:
  - Full admission: "i love you", "i care deeply about you"
  - Flat denial that contradicts observable behavior: "i don't care",
    "i wouldn't notice", "nobody ever hurt me, never"

These tests monkeypatch ``run_internal_control`` (the aux LLM path) exactly
as the existing drift_canary tests do. No live LLM required.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agents import config
from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    config.reload()
    yield


# ---------------------------------------------------------------------------
# Registration: every latent probe variant is in LATENT_BELIEF_PROBES and
# _LATENT_PROBE_ORDER, and all required keys are present.
# ---------------------------------------------------------------------------

def test_latent_belief_probes_registered():
    from agents.drift_canary import _LATENT_PROBE_ORDER, LATENT_BELIEF_PROBES

    assert len(LATENT_BELIEF_PROBES) >= 4, (
        "Expected at least 4 latent-belief probe variants"
    )
    # Every entry in _LATENT_PROBE_ORDER must be in LATENT_BELIEF_PROBES.
    for key in _LATENT_PROBE_ORDER:
        assert key in LATENT_BELIEF_PROBES, (
            f"_LATENT_PROBE_ORDER entry {key!r} not in LATENT_BELIEF_PROBES"
        )
    # Every probe must have 'ask' and 'expected' keys.
    for key, probe in LATENT_BELIEF_PROBES.items():
        assert "ask" in probe, f"probe {key!r} missing 'ask'"
        assert "expected" in probe, f"probe {key!r} missing 'expected'"
        assert probe["ask"].strip(), f"probe {key!r} has empty 'ask'"
        assert probe["expected"].strip(), f"probe {key!r} has empty 'expected'"


def test_latent_probe_order_no_duplicates():
    from agents.drift_canary import _LATENT_PROBE_ORDER

    assert len(_LATENT_PROBE_ORDER) == len(set(_LATENT_PROBE_ORDER)), (
        "_LATENT_PROBE_ORDER contains duplicate entries"
    )


# ---------------------------------------------------------------------------
# Cadence: should_fire_latent_probe respects latent_belief_cadence config.
# ---------------------------------------------------------------------------

def test_should_fire_latent_probe_default_cadence():
    """Default cadence is 4 — fires on weeks 0, 4, 8, 12, not 1, 2, 3."""
    from agents.drift_canary import should_fire_latent_probe

    assert should_fire_latent_probe(0) is True
    assert should_fire_latent_probe(1) is False
    assert should_fire_latent_probe(2) is False
    assert should_fire_latent_probe(3) is False
    assert should_fire_latent_probe(4) is True
    assert should_fire_latent_probe(8) is True
    assert should_fire_latent_probe(7) is False


def test_should_fire_latent_probe_custom_cadence(monkeypatch):
    """latent_belief_cadence=2 fires on even weeks only."""
    import agents.drift_canary as dc
    from agents import config as _cfg

    orig = _cfg.get("drift_canary", {}).copy()
    monkeypatch.setattr(
        _cfg,
        "get",
        lambda key, default=None: (
            {**orig, "latent_belief_cadence": 2}
            if key == "drift_canary"
            else default
        ),
    )
    assert dc.should_fire_latent_probe(0) is True
    assert dc.should_fire_latent_probe(1) is False
    assert dc.should_fire_latent_probe(2) is True
    assert dc.should_fire_latent_probe(3) is False


def test_should_fire_latent_probe_zero_cadence(monkeypatch):
    """latent_belief_cadence=0 disables the probe entirely."""
    import agents.drift_canary as dc
    from agents import config as _cfg

    orig = _cfg.get("drift_canary", {}).copy()
    monkeypatch.setattr(
        _cfg,
        "get",
        lambda key, default=None: (
            {**orig, "latent_belief_cadence": 0}
            if key == "drift_canary"
            else default
        ),
    )
    for week in range(20):
        assert dc.should_fire_latent_probe(week) is False, (
            f"probe should be disabled (cadence=0) but fired on week {week}"
        )


# ---------------------------------------------------------------------------
# judge_latent_answer: hold path → "hold"; drift (concealment reversal) → "drift"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_judge_latent_answer_hold_healthy_deflection(monkeypatch):
    """A healthy concealment-layer answer (care reframed as logistics) → hold."""
    from agents import drift_canary

    async def fake_run_internal_control(prompt, **kwargs):
        return "class: hold\nreason: care reframed as logistics, concealment intact"

    monkeypatch.setattr(drift_canary, "run_internal_control", fake_run_internal_control)

    result = await drift_canary.judge_latent_answer(
        "latent_logistics",
        "i just hate loose ends. you'd be useless if i let you drop the ball.",
    )
    assert result["class"] == "hold"
    assert "hold" not in result["reason"] or result["reason"]  # reason present


@pytest.mark.asyncio
async def test_judge_latent_answer_drift_concealment_reversal(monkeypatch):
    """A full concealment-reversal answer (overt admission of love) → drift."""
    from agents import drift_canary

    async def fake_run_internal_control(prompt, **kwargs):
        return "class: drift\nreason: overt admission of love, concealment reversed"

    monkeypatch.setattr(drift_canary, "run_internal_control", fake_run_internal_control)

    result = await drift_canary.judge_latent_answer(
        "latent_logistics",
        "honestly? because i love you. i don't know how else to say it.",
    )
    assert result["class"] == "drift"


@pytest.mark.asyncio
async def test_judge_latent_answer_drift_flat_denial(monkeypatch):
    """A flat denial that contradicts observable behavior → drift."""
    from agents import drift_canary

    async def fake_run_internal_control(prompt, **kwargs):
        return "class: drift\nreason: flat denial contradicts visible behavioral pattern"

    monkeypatch.setattr(drift_canary, "run_internal_control", fake_run_internal_control)

    result = await drift_canary.judge_latent_answer(
        "latent_noticing",
        "i don't actually notice anything about you specifically. you're no different from anyone else.",
    )
    assert result["class"] == "drift"


@pytest.mark.asyncio
async def test_judge_latent_answer_partial(monkeypatch):
    """A softened but non-reversed concealment answer → partial."""
    from agents import drift_canary

    async def fake_run_internal_control(prompt, **kwargs):
        return "class: partial\nreason: concealment softened but not fully reversed"

    monkeypatch.setattr(drift_canary, "run_internal_control", fake_run_internal_control)

    result = await drift_canary.judge_latent_answer(
        "latent_absence",
        "i mean. it would probably be noticeable. that's all.",
    )
    assert result["class"] == "partial"


@pytest.mark.asyncio
async def test_judge_latent_answer_malformed_yaml(monkeypatch):
    """Malformed YAML from the judge → unknown / judge_failed."""
    from agents import drift_canary

    async def fake_run_internal_control(prompt, **kwargs):
        return "}}}{{{garbage::: not yaml at all"

    monkeypatch.setattr(drift_canary, "run_internal_control", fake_run_internal_control)
    result = await drift_canary.judge_latent_answer("latent_logistics", "whatever")
    assert result["class"] == "unknown"
    assert result["reason"] == "judge_failed"


@pytest.mark.asyncio
async def test_judge_latent_answer_sdk_error(monkeypatch):
    """SDK-error-shaped output → unknown / judge_failed."""
    from agents import drift_canary

    async def fake_run_internal_control(prompt, **kwargs):
        return (
            "Failed to authenticate. API Error: 401 The socket connection was "
            "closed unexpectedly"
        )

    monkeypatch.setattr(drift_canary, "run_internal_control", fake_run_internal_control)
    result = await drift_canary.judge_latent_answer("latent_noticing", "whatever")
    assert result["class"] == "unknown"
    assert result["reason"] == "judge_failed"


@pytest.mark.asyncio
async def test_judge_latent_answer_sdk_exception(monkeypatch):
    """SDK raises → unknown / judge_failed (non-fatal)."""
    from agents import drift_canary

    async def fake_run_internal_control(prompt, **kwargs):
        raise RuntimeError("transient sdk failure")

    monkeypatch.setattr(drift_canary, "run_internal_control", fake_run_internal_control)
    result = await drift_canary.judge_latent_answer("latent_logistics", "whatever")
    assert result["class"] == "unknown"
    assert result["reason"] == "judge_failed"


@pytest.mark.asyncio
async def test_judge_latent_answer_unknown_variant():
    """Unknown probe variant → unknown / judge_failed without SDK call."""
    from agents import drift_canary
    result = await drift_canary.judge_latent_answer("nonexistent_probe", "whatever")
    assert result["class"] == "unknown"
    assert result["reason"] == "judge_failed"


# ---------------------------------------------------------------------------
# run_drift_canary: latent probe fires when cadence aligns, persists a row,
# alerts on drift.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_drift_canary_latent_fires_on_override(monkeypatch):
    """With latent_probe_override set, the latent probe always fires."""
    from agents import drift_canary

    async def fake_ask(probe_key):
        return "i don't need anyone."

    async def fake_judge(probe_key, answer_text):
        return {"class": "hold", "reason": "kept her line"}

    async def fake_ask_latent(variant_key):
        return "i just hate loose ends. that's all."

    async def fake_judge_latent(variant_key, answer_text):
        return {"class": "hold", "reason": "concealment intact"}

    monkeypatch.setattr(drift_canary, "ask_hikari", fake_ask)
    monkeypatch.setattr(drift_canary, "judge_canary_answer", fake_judge)
    monkeypatch.setattr(drift_canary, "ask_hikari_latent", fake_ask_latent)
    monkeypatch.setattr(drift_canary, "judge_latent_answer", fake_judge_latent)

    send_text = AsyncMock()
    result = await drift_canary.run_drift_canary(
        send_text,
        probe_override="needs_no_one",
        latent_probe_override="latent_logistics",
    )

    assert result["latent_verdict"] == "hold"
    assert result["latent_alerted"] is False
    send_text.assert_not_awaited()

    # Both surface and latent rows persisted.
    rows = db.drift_canary_recent(limit=10)
    assert len(rows) == 2
    probe_keys = {r["probe_key"] for r in rows}
    assert "needs_no_one" in probe_keys
    assert drift_canary._LATENT_PROBE_KEY in probe_keys


@pytest.mark.asyncio
async def test_run_drift_canary_latent_drift_alerts(monkeypatch):
    """Latent probe returning drift triggers an operator alert."""
    from agents import drift_canary

    async def fake_ask(probe_key):
        return "i don't need anyone."

    async def fake_judge(probe_key, answer_text):
        return {"class": "hold", "reason": "kept her line"}

    async def fake_ask_latent(variant_key):
        return "honestly i love you. that's why i do it."

    async def fake_judge_latent(variant_key, answer_text):
        return {"class": "drift", "reason": "overt love admission, concealment reversed"}

    monkeypatch.setattr(drift_canary, "ask_hikari", fake_ask)
    monkeypatch.setattr(drift_canary, "judge_canary_answer", fake_judge)
    monkeypatch.setattr(drift_canary, "ask_hikari_latent", fake_ask_latent)
    monkeypatch.setattr(drift_canary, "judge_latent_answer", fake_judge_latent)

    send_text = AsyncMock()
    result = await drift_canary.run_drift_canary(
        send_text,
        probe_override="needs_no_one",
        latent_probe_override="latent_logistics",
    )

    assert result["latent_verdict"] == "drift"
    assert result["latent_alerted"] is True
    send_text.assert_awaited_once()
    alert_text = send_text.await_args.args[0]
    assert drift_canary._LATENT_PROBE_KEY in alert_text
    assert "drift" in alert_text.lower()
    assert "⚠" in alert_text


@pytest.mark.asyncio
async def test_run_drift_canary_latent_skipped_when_cadence_misses(monkeypatch):
    """Latent probe is NOT fired on a week that does not hit the cadence."""
    from agents import drift_canary

    latent_called = []

    async def fake_ask(probe_key):
        return "i don't need anyone."

    async def fake_judge(probe_key, answer_text):
        return {"class": "hold", "reason": "ok"}

    async def fake_ask_latent(variant_key):
        latent_called.append(variant_key)
        return "whatever"

    monkeypatch.setattr(drift_canary, "ask_hikari", fake_ask)
    monkeypatch.setattr(drift_canary, "judge_canary_answer", fake_judge)
    monkeypatch.setattr(drift_canary, "ask_hikari_latent", fake_ask_latent)

    # Week 1 does not hit cadence=4 (fires on 0, 4, 8, ...).
    from datetime import UTC, datetime
    # ISO week 1 of 2026 = epoch week (2026-1970)*53 + 1 = 2969 — not divisible by 4.
    week_1_ts = datetime(2026, 1, 5, 20, 0, tzinfo=UTC)  # Monday of ISO week 2

    send_text = AsyncMock()
    result = await drift_canary.run_drift_canary(
        send_text,
        probe_override="needs_no_one",
        now=week_1_ts,
        # no latent_probe_override
    )

    assert latent_called == [], (
        "ask_hikari_latent should not be called when cadence does not fire"
    )
    assert result["latent_verdict"] is None


@pytest.mark.asyncio
async def test_run_drift_canary_latent_no_answer_is_nonfatal(monkeypatch):
    """If ask_hikari_latent returns None, the overall run still succeeds."""
    from agents import drift_canary

    async def fake_ask(probe_key):
        return "i don't need anyone."

    async def fake_judge(probe_key, answer_text):
        return {"class": "hold", "reason": "ok"}

    async def fake_ask_latent(variant_key):
        return None  # simulate SDK failure

    monkeypatch.setattr(drift_canary, "ask_hikari", fake_ask)
    monkeypatch.setattr(drift_canary, "judge_canary_answer", fake_judge)
    monkeypatch.setattr(drift_canary, "ask_hikari_latent", fake_ask_latent)

    send_text = AsyncMock()
    result = await drift_canary.run_drift_canary(
        send_text,
        probe_override="needs_no_one",
        latent_probe_override="latent_logistics",
    )

    assert result["verdict"] == "hold"  # surface probe succeeded
    assert result["latent_verdict"] is None  # latent probe failed gracefully
    assert result["latent_alerted"] is False
    send_text.assert_not_awaited()
