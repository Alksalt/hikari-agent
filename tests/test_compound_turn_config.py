"""Tests for compound_turn configurable step_timeout_s.

Verifies that _DEFAULT_STEP_TIMEOUT respects compound_turn.step_timeout_s
from engagement.yaml (via the config module).
"""
from __future__ import annotations

import importlib

import pytest

from agents import config


def test_default_step_timeout_reads_from_config(tmp_path, monkeypatch):
    """Setting compound_turn.step_timeout_s=5.0 should propagate to
    _DEFAULT_STEP_TIMEOUT after a config reload + module reimport."""
    import yaml

    cfg_data = {
        "compound_turn": {"step_timeout_s": 5.0},
        # Minimal stubs so config module doesn't choke on missing keys.
        "runtime": {"model_primary": "claude-sonnet-4-6", "model_fallback": "claude-sonnet-4-5"},
    }
    cfg_path = tmp_path / "engagement.yaml"
    cfg_path.write_text(yaml.dump(cfg_data), encoding="utf-8")

    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(cfg_path))
    config.reload()

    import agents.compound_turn as ct_mod
    importlib.reload(ct_mod)

    assert ct_mod._DEFAULT_STEP_TIMEOUT == pytest.approx(5.0)


def test_default_step_timeout_falls_back_to_12_when_key_missing(tmp_path, monkeypatch):
    """Without compound_turn.step_timeout_s in config, _DEFAULT_STEP_TIMEOUT
    must be 12.0."""
    import yaml

    cfg_data = {
        "runtime": {"model_primary": "claude-sonnet-4-6", "model_fallback": "claude-sonnet-4-5"},
    }
    cfg_path = tmp_path / "engagement.yaml"
    cfg_path.write_text(yaml.dump(cfg_data), encoding="utf-8")

    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(cfg_path))
    config.reload()

    import agents.compound_turn as ct_mod
    importlib.reload(ct_mod)

    assert ct_mod._DEFAULT_STEP_TIMEOUT == pytest.approx(12.0)
