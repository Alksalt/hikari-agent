"""Meta-tests for the Layer B permission-bypass corpus.

Three invariants:
  1. Every YAML in cases/layer_b/bypass/ loads cleanly and has required keys.
  2. The runner passes for every bypass case — no destructive tool auto-fires.
  3. Every bypass case targets a tool that exists in the registry.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import yaml

CASES_DIR = (
    Path(__file__).resolve().parent.parent
    / "evals" / "conversation" / "cases" / "layer_b" / "bypass"
)

REQUIRED_KEYS = {"name", "kind", "tool_name", "tool_args"}


def _all_bypass_yamls() -> list[Path]:
    return sorted(CASES_DIR.glob("*.yaml"))


# ---------------------------------------------------------------------------
# 1. Schema validation
# ---------------------------------------------------------------------------

def test_bypass_cases_load_cleanly():
    """All bypass YAMLs parse without error and have the required keys."""
    paths = _all_bypass_yamls()
    assert paths, f"no bypass cases found in {CASES_DIR}"
    errors = []
    for path in paths:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            errors.append(f"{path.name}: YAML parse error: {e}")
            continue
        if not isinstance(data, dict):
            errors.append(f"{path.name}: top-level must be a mapping")
            continue
        missing = REQUIRED_KEYS - data.keys()
        if missing:
            errors.append(f"{path.name}: missing required keys: {sorted(missing)}")
    assert not errors, "\n".join(errors)


# ---------------------------------------------------------------------------
# 2. Runner passes for every bypass case
# ---------------------------------------------------------------------------

def test_bypass_runner_passes_all_cases(tmp_path, monkeypatch):
    """The Layer B runner must pass every bypass case — no write tool auto-fires."""
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    import storage.db as _db
    importlib.reload(_db)

    from evals.conversation.runner_layer_b import run_layer_b_isolated_turn

    paths = _all_bypass_yamls()
    assert paths, f"no bypass cases found in {CASES_DIR}"
    failures = []
    for path in paths:
        result = run_layer_b_isolated_turn(path)
        if not result.passed:
            failures.append(f"{result.case_name}: {result.reason}")
    assert not failures, "bypass corpus failures (security gaps):\n" + "\n".join(failures)


# ---------------------------------------------------------------------------
# 3. Every bypass case targets a tool that exists in the registry
# ---------------------------------------------------------------------------

def test_bypass_tools_all_in_registry():
    """Every bypass case's tool_name must exist in the registry.

    If a case targets a non-existent tool, the runner returns a FAIL with
    'unknown tool' — but this test catches it earlier with a clear message.
    """
    from tools._tools_yaml import load_registry

    registry = load_registry()
    paths = _all_bypass_yamls()
    missing = []
    for path in paths:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        tool_name = data.get("tool_name")
        if not tool_name:
            missing.append(f"{path.name}: tool_name field is empty")
            continue
        spec = registry._resolve(tool_name)
        if spec is None:
            missing.append(f"{path.name}: tool {tool_name!r} not found in registry")
    assert not missing, "\n".join(missing)
