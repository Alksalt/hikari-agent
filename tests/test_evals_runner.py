"""Smoke tests for the eval harness."""
from __future__ import annotations

from pathlib import Path

import pytest

from evals.conversation import load_case, run_layer_a
from evals.conversation.runner import run_layer_b

CASES_DIR = Path(__file__).resolve().parent.parent / "evals" / "conversation" / "cases"


def test_load_case_invalid_yaml(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("not: valid: yaml: here:")
    with pytest.raises(ValueError):
        load_case(p)


def test_load_case_missing_slug(tmp_path):
    p = tmp_path / "nosllug.yaml"
    p.write_text("layer: a\n")
    with pytest.raises(ValueError, match="missing 'slug'"):
        load_case(p)


def test_load_case_invalid_layer(tmp_path):
    p = tmp_path / "badlayer.yaml"
    p.write_text("slug: x\nlayer: zz\n")
    with pytest.raises(ValueError, match="layer must be one of"):
        load_case(p)


def test_layer_a_all_cases_pass():
    layer_a = CASES_DIR / "layer_a"
    case_files = sorted(layer_a.glob("*.yaml"))
    assert case_files, "no Layer A cases found"
    failures = []
    for path in case_files:
        case = load_case(path)
        result = run_layer_a(case)
        if not result.passed:
            failures.append(f"{case.slug}: {result.failures}")
    assert not failures, "\n".join(failures)


def test_layer_b_runs_without_error():
    """Layer B runner returns (passed, total, errors) tuple without raising."""
    layer_b = CASES_DIR / "layer_b"
    assert layer_b.exists(), "layer_b cases dir not found"
    passed, total, errors = run_layer_b(layer_b)
    assert total > 0, "no Layer B cases discovered"
    assert errors == [], f"Layer B failures: {errors}"


def test_layer_c_discovers_cases():
    """Layer C cases dir has golden and cadence subdirs, each with at least one YAML."""
    layer_c = CASES_DIR / "layer_c"
    assert (layer_c / "golden").exists(), "golden subdir missing"
    assert (layer_c / "cadence").exists(), "cadence subdir missing"
    golden = sorted((layer_c / "golden").glob("*.yaml"))
    cadence = sorted((layer_c / "cadence").glob("*.yaml"))
    assert len(golden) >= 1, "no golden cases found"
    assert len(cadence) >= 1, "no cadence cases found"


def test_layer_c_cadence_runs_deterministically():
    """Layer C cadence cases run without error and return a result."""
    from evals.conversation.runner_layer_c import run_layer_c_cadence
    layer_c = CASES_DIR / "layer_c"
    cadence_cases = sorted((layer_c / "cadence").glob("*.yaml"))
    assert cadence_cases, "no cadence cases found"
    for case_path in cadence_cases:
        result = run_layer_c_cadence(case_path)
        # Cadence cases should pass (deterministic, no external deps).
        assert result.passed is True, f"{result.case_name}: {result.reason}"
