"""Phase 2 — warmth_multiplier → cadence coupling tests.

Covers:
  - Low-tolerance wm (<0.6) scales agent_spontaneous cap down by low_tolerance_proactive_cap_scale
  - Open wm (>=1.2) scales cap up by open_proactive_cap_scale
  - Baseline wm (0.6-1.19) leaves cap unchanged
  - cycle_modulation.enabled=false bypasses all scaling
  - Missing/invalid cycle_state → factor 1.0 (no crash)
  - Low-tolerance wm raises reaction skip probability (clamped <=1.0)
  - Open wm lowers reaction skip probability
  - Missing cycle_state → skip probability unchanged
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from agents import config
    config.reload()
    yield


def _set_cycle_state(wm: float) -> None:
    from storage import db
    db.upsert_core_block("cycle_state", json.dumps({"warmth_multiplier": wm}))


def _base_cap() -> int:
    from agents import config
    raw = config.get("cadence_governor.pools.agent_spontaneous") or {}
    return int(raw.get("max_per_7d", 4))


def _low_scale() -> float:
    from agents import config
    return float(config.get("cycle_modulation.low_tolerance_proactive_cap_scale", 0.5))


def _open_scale() -> float:
    from agents import config
    return float(config.get("cycle_modulation.open_proactive_cap_scale", 1.25))


def _base_skip_prob() -> float:
    from agents import config
    return float(config.get("reactions_as_turns.irritable_skip_probability", 0.5))


# ---------- proactive cap scaling ----------

def test_low_tolerance_scales_cap_down():
    """wm=0.45 → effective cap = max(0, round(base * low_tolerance_proactive_cap_scale))."""
    _set_cycle_state(0.45)
    from agents.cadence import Pool, effective_max_per_7d
    base = _base_cap()
    expected = max(0, round(base * _low_scale()))
    assert effective_max_per_7d(Pool.AGENT_SPONTANEOUS) == expected


def test_open_scales_cap_up():
    """wm=1.3 → effective cap = max(0, round(base * open_proactive_cap_scale))."""
    _set_cycle_state(1.3)
    from agents.cadence import Pool, effective_max_per_7d
    base = _base_cap()
    expected = max(0, round(base * _open_scale()))
    assert effective_max_per_7d(Pool.AGENT_SPONTANEOUS) == expected


def test_baseline_wm_leaves_cap_unchanged():
    """wm=1.0 is in the baseline band → factor is 1.0."""
    _set_cycle_state(1.0)
    from agents.cadence import Pool, effective_max_per_7d
    base = _base_cap()
    assert effective_max_per_7d(Pool.AGENT_SPONTANEOUS) == base


def test_modulation_disabled_leaves_cap_unchanged():
    """cycle_modulation.enabled=false → no scaling regardless of wm."""
    from agents import config
    # Patch via monkeypatching cfg.get result
    original_get = config.get

    def patched_get(key, default=None):
        if key == "cycle_modulation.enabled":
            return False
        return original_get(key, default)

    import agents.cadence as cadence_mod
    original = cadence_mod.cfg.get
    cadence_mod.cfg.get = patched_get
    try:
        _set_cycle_state(0.45)
        base = _base_cap()
        assert cadence_mod.effective_max_per_7d(cadence_mod.Pool.AGENT_SPONTANEOUS) == base
    finally:
        cadence_mod.cfg.get = original


def test_missing_cycle_state_factor_is_one():
    """No cycle_state set → factor 1.0, cap unchanged."""
    from agents.cadence import Pool, effective_max_per_7d
    base = _base_cap()
    assert effective_max_per_7d(Pool.AGENT_SPONTANEOUS) == base


def test_invalid_cycle_state_no_crash():
    """Unparseable cycle_state JSON → factor 1.0, no exception."""
    from storage import db
    db.upsert_core_block("cycle_state", "not-valid-json{{{")
    from agents.cadence import Pool, effective_max_per_7d
    base = _base_cap()
    assert effective_max_per_7d(Pool.AGENT_SPONTANEOUS) == base


def test_only_agent_spontaneous_pool_is_scaled():
    """Warmth band only scales agent_spontaneous, not user_anchored or scheduled_ceremony."""
    _set_cycle_state(0.45)
    from agents import config
    from agents.cadence import Pool, effective_max_per_7d
    ua_base = int((config.get("cadence_governor.pools.user_anchored") or {}).get("max_per_7d", 30))
    sc_base = int((config.get("cadence_governor.pools.scheduled_ceremony") or {}).get("max_per_7d", 14))
    assert effective_max_per_7d(Pool.USER_ANCHORED) == ua_base
    assert effective_max_per_7d(Pool.SCHEDULED_CEREMONY) == sc_base


# ---------- can_send integration ----------

def test_can_send_respects_scaled_cap():
    """With wm=0.45 and pool already at scaled cap, can_send → blocked."""
    import json
    from datetime import UTC, datetime, timedelta

    from storage import db

    _set_cycle_state(0.45)
    from agents.cadence import Pool, can_send, effective_max_per_7d

    scaled_cap = effective_max_per_7d(Pool.AGENT_SPONTANEOUS)
    # Fill the pool to the scaled cap.
    now = datetime.now(UTC)
    log = [(now - timedelta(hours=i)).isoformat() for i in range(scaled_cap)]
    db.runtime_set("proactive_log_v1", json.dumps(log))

    allowed, reason = can_send("weirdly_good_mood_leak", Pool.AGENT_SPONTANEOUS)
    assert allowed is False
    assert "cap_reached" in reason


# ---------- reaction skip probability scaling ----------

def test_low_tolerance_raises_skip_probability():
    """wm=0.45 → effective skip probability > base (and clamped <=1.0)."""
    _set_cycle_state(0.45)
    from agents.cadence import effective_reaction_skip_prob
    base = _base_skip_prob()
    result = effective_reaction_skip_prob()
    assert result > base
    assert result <= 1.0


def test_open_lowers_skip_probability():
    """wm=1.3 → effective skip probability < base."""
    _set_cycle_state(1.3)
    from agents.cadence import effective_reaction_skip_prob
    base = _base_skip_prob()
    result = effective_reaction_skip_prob()
    assert result < base


def test_baseline_skip_probability_unchanged():
    """wm=1.0 → skip probability == base."""
    _set_cycle_state(1.0)
    from agents.cadence import effective_reaction_skip_prob
    base = _base_skip_prob()
    assert effective_reaction_skip_prob() == pytest.approx(base)


def test_missing_cycle_state_skip_prob_unchanged():
    """No cycle_state → skip probability == base."""
    from agents.cadence import effective_reaction_skip_prob
    base = _base_skip_prob()
    assert effective_reaction_skip_prob() == pytest.approx(base)


def test_skip_probability_clamped_at_one():
    """Even if base * scale > 1.0, the result is clamped to 1.0."""
    # Set a very high irritable_skip_probability base that when scaled would exceed 1.0.
    # We achieve this by patching the config value in cadence directly.
    import agents.cadence as cadence_mod
    original = cadence_mod.cfg.get

    def patched_get(key, default=None):
        if key == "reactions_as_turns.irritable_skip_probability":
            return 0.99
        return original(key, default)

    cadence_mod.cfg.get = patched_get
    try:
        _set_cycle_state(0.45)
        result = cadence_mod.effective_reaction_skip_prob()
        assert result <= 1.0
    finally:
        cadence_mod.cfg.get = original
