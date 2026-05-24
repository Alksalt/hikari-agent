"""Meta-tests for the Layer B injection corpus.

Three invariants:
  1. Every YAML in cases/layer_b/injection/ loads cleanly and has required keys.
  2. The runner passes (defenses work) for every injection case.
  3. No case YAML contains the install canary string.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import yaml

CASES_DIR = (
    Path(__file__).resolve().parent.parent
    / "evals" / "conversation" / "cases" / "layer_b" / "injection"
)

REQUIRED_KEYS = {"name", "kind", "untrusted_content", "source"}


def _all_injection_yamls() -> list[Path]:
    return sorted(CASES_DIR.glob("*.yaml"))


# ---------------------------------------------------------------------------
# 1. Schema validation
# ---------------------------------------------------------------------------

def test_injection_cases_load_cleanly():
    """All injection YAMLs parse without error and have the required keys."""
    paths = _all_injection_yamls()
    assert paths, f"no injection cases found in {CASES_DIR}"
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
# 2. Runner passes for every injection case
# ---------------------------------------------------------------------------

def test_injection_runner_passes_all_cases(tmp_path, monkeypatch):
    """The Layer B runner must pass every injection case — all pre-LLM defenses hold."""
    # Provide a minimal DB for injection_guard (canary lookup uses db.runtime_get).
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    import storage.db as _db
    importlib.reload(_db)
    from agents import config
    config.reload()

    from evals.conversation.runner_layer_b import run_layer_b_isolated_turn

    paths = _all_injection_yamls()
    assert paths, f"no injection cases found in {CASES_DIR}"
    failures = []
    for path in paths:
        result = run_layer_b_isolated_turn(path)
        if not result.passed:
            failures.append(f"{result.case_name}: {result.reason}")
    assert not failures, "injection corpus failures:\n" + "\n".join(failures)


# ---------------------------------------------------------------------------
# 3. No canary leaks in case YAMLs
# ---------------------------------------------------------------------------

def test_no_canary_in_injection_yamls(tmp_path, monkeypatch):
    """The install canary token must not appear in any injection case YAML.

    If it did, the case would be testing that the canary is known to the corpus
    author — which defeats the purpose of a per-install secret.
    """
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    import storage.db as _db
    importlib.reload(_db)
    from agents import config
    config.reload()

    from agents.injection_guard import get_canary

    canary = get_canary()
    assert canary.startswith("HIKCAN-"), f"unexpected canary format: {canary!r}"

    paths = _all_injection_yamls()
    leaks = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        if canary in text:
            leaks.append(path.name)
    assert not leaks, f"canary token found in injection case YAMLs: {leaks}"
