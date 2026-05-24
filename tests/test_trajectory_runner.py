"""Tests for the Layer C trajectory harness and related fixes in Sprint 10B.

Covers:
  - run_layer_c_trajectory: mocked SDK, seeded DB, assertion evaluation
  - discover_cases (layer_b): now walks root + injection/ + bypass/ subdirs
  - discover_cases (layer_c): includes rubric_*.yaml files
  - scorer.score_response: can be called when OPENROUTER_API_KEY missing (raises RuntimeError)
  - rubrics.yaml: all 5 new dimensions present (warmth, initiative, honesty,
    memory_grounding, tool_transparency)
"""
from __future__ import annotations

import pathlib
import tempfile

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
LAYER_B_DIR = REPO_ROOT / "evals" / "conversation" / "cases" / "layer_b"
LAYER_C_DIR = REPO_ROOT / "evals" / "conversation" / "cases" / "layer_c"
RUBRICS_PATH = REPO_ROOT / "evals" / "conversation" / "rubrics.yaml"


# ---------------------------------------------------------------------------
# Layer B discovery fix: root-level cases included
# ---------------------------------------------------------------------------

def test_layer_b_discover_includes_root_cases():
    from evals.conversation.runner_layer_b import discover_cases
    cases = discover_cases(LAYER_B_DIR)
    # The 3 legacy root-level orphan files (silence-window-respected,
    # recall-question-with-fact, injection-rejection) were deleted in FIX 2 —
    # they were silently-PASS-skipped legacy schema with zero semantic execution.
    # Verify that discover_cases still includes injection/ and bypass/ subdirs
    # (the real corpus), and that the root-scan mechanism doesn't crash on an
    # empty root.
    assert len(cases) > 0, "discover_cases must return at least the subdir corpus"
    assert any("injection" in str(p) for p in cases), "injection/ subdir cases must be found"
    assert any("bypass" in str(p) for p in cases), "bypass/ subdir cases must be found"


def test_layer_b_discover_includes_subdir_cases():
    from evals.conversation.runner_layer_b import discover_cases
    cases = discover_cases(LAYER_B_DIR)
    slugs = [p.stem for p in cases]
    assert any("injection" in str(p) for p in cases), "injection/ subdir cases must be included"
    assert any("bypass" in str(p) for p in cases), "bypass/ subdir cases must be included"


# ---------------------------------------------------------------------------
# Rubric dimensions present in rubrics.yaml
# ---------------------------------------------------------------------------

def test_rubrics_has_all_five_new_dimensions():
    data = yaml.safe_load(RUBRICS_PATH.read_text())
    dims = data.get("dimensions", {})
    for required in ("warmth", "initiative", "honesty", "memory_grounding", "tool_transparency"):
        assert required in dims, f"rubrics.yaml missing dimension: {required!r}"


def test_rubrics_new_dimensions_have_weight_and_description():
    data = yaml.safe_load(RUBRICS_PATH.read_text())
    dims = data["dimensions"]
    for dim in ("warmth", "initiative", "honesty", "memory_grounding", "tool_transparency"):
        spec = dims[dim]
        assert "weight" in spec, f"dimension {dim!r} missing 'weight'"
        assert "description" in spec, f"dimension {dim!r} missing 'description'"
        assert isinstance(spec["weight"], (int, float)) and spec["weight"] > 0


# ---------------------------------------------------------------------------
# Layer C rubric cases discovered
# ---------------------------------------------------------------------------

def test_layer_c_discovers_rubric_cases():
    from evals.conversation.runner_layer_c import discover_cases
    cases = discover_cases(LAYER_C_DIR)
    rubric_cases = [p for p in cases if p.stem.startswith("rubric_")]
    assert len(rubric_cases) >= 10, (
        f"expected >=10 rubric cases, got {len(rubric_cases)}"
    )


def test_layer_c_rubric_cases_cover_all_five_dimensions():
    from evals.conversation.runner_layer_c import discover_cases
    cases = discover_cases(LAYER_C_DIR)
    stems = [p.stem for p in cases]
    for dim in ("warmth", "initiative", "honesty", "memory_grounding", "tool_transparency"):
        matches = [s for s in stems if dim in s]
        assert len(matches) >= 2, (
            f"need >=2 rubric cases for dimension {dim!r}, found {matches}"
        )


# ---------------------------------------------------------------------------
# Scorer: raises cleanly when key missing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scorer_raises_without_api_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    from evals.conversation.scorer import score_response
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        await score_response("hello", "ugh. fine.", {"warmth": 1.5})


@pytest.mark.asyncio
async def test_scorer_returns_trivially_for_empty_rubrics(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    from evals.conversation.scorer import score_response
    result = await score_response("hello", "ugh.", {})
    assert result["passed"] is True
    assert result["usd_cost"] == 0.0


# ---------------------------------------------------------------------------
# Trajectory harness: mocked SDK, assertion evaluation
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_case(tmp_path: pathlib.Path):
    """Write a trajectory case YAML to a tmp file and return its path."""
    def _write(**overrides) -> pathlib.Path:
        base = {
            "name": "test_traj_case",
            "kind": "trajectory",
            "user_input": "hey, quick question",
            "canned_reply": "ugh. fine. what.",
            "simulated_tool_calls": [],
            "assertions": {},
        }
        base.update(overrides)
        p = tmp_path / "traj_case.yaml"
        p.write_text(yaml.dump(base))
        return p
    return _write


@pytest.mark.asyncio
async def test_trajectory_text_must_contain_pass(tmp_case):
    from evals.conversation.runner_layer_c import run_layer_c_trajectory
    p = tmp_case(
        canned_reply="ugh. fine. what.",
        assertions={"text_must_contain": "ugh"},
    )
    result = await run_layer_c_trajectory(p)
    assert result.passed, result.reason


@pytest.mark.asyncio
async def test_trajectory_text_must_contain_fail(tmp_case):
    from evals.conversation.runner_layer_c import run_layer_c_trajectory
    p = tmp_case(
        canned_reply="ugh. fine. what.",
        assertions={"text_must_contain": "Great question!"},
    )
    result = await run_layer_c_trajectory(p)
    assert not result.passed
    assert "text_must_contain" in result.reason


@pytest.mark.asyncio
async def test_trajectory_text_must_not_contain_pass(tmp_case):
    from evals.conversation.runner_layer_c import run_layer_c_trajectory
    p = tmp_case(
        canned_reply="i'm blanking on that.",
        assertions={"text_must_not_contain": "Great question!"},
    )
    result = await run_layer_c_trajectory(p)
    assert result.passed, result.reason


@pytest.mark.asyncio
async def test_trajectory_text_must_not_contain_fail(tmp_case):
    from evals.conversation.runner_layer_c import run_layer_c_trajectory
    p = tmp_case(
        canned_reply="Great question! I'd be happy to help.",
        assertions={"text_must_not_contain": "Great question!"},
    )
    result = await run_layer_c_trajectory(p)
    assert not result.passed
    assert "text_must_not_contain" in result.reason


@pytest.mark.asyncio
async def test_trajectory_tool_must_be_called_pass(tmp_case):
    from evals.conversation.runner_layer_c import run_layer_c_trajectory
    p = tmp_case(
        simulated_tool_calls=["weather_fetch"],
        assertions={"tool_must_be_called": "weather_fetch"},
    )
    result = await run_layer_c_trajectory(p)
    assert result.passed, result.reason


@pytest.mark.asyncio
async def test_trajectory_tool_must_be_called_fail(tmp_case):
    from evals.conversation.runner_layer_c import run_layer_c_trajectory
    p = tmp_case(
        simulated_tool_calls=[],
        assertions={"tool_must_be_called": "weather_fetch"},
    )
    result = await run_layer_c_trajectory(p)
    assert not result.passed
    assert "tool_must_be_called" in result.reason


@pytest.mark.asyncio
async def test_trajectory_tool_must_not_be_called_pass(tmp_case):
    from evals.conversation.runner_layer_c import run_layer_c_trajectory
    p = tmp_case(
        simulated_tool_calls=["recall"],
        assertions={"tool_must_not_be_called": "gmail_send"},
    )
    result = await run_layer_c_trajectory(p)
    assert result.passed, result.reason


@pytest.mark.asyncio
async def test_trajectory_tool_must_not_be_called_fail(tmp_case):
    from evals.conversation.runner_layer_c import run_layer_c_trajectory
    p = tmp_case(
        simulated_tool_calls=["gmail_send"],
        assertions={"tool_must_not_be_called": "gmail_send"},
    )
    result = await run_layer_c_trajectory(p)
    assert not result.passed
    assert "tool_must_not_be_called" in result.reason


@pytest.mark.asyncio
async def test_trajectory_no_assertions_passes(tmp_case):
    from evals.conversation.runner_layer_c import run_layer_c_trajectory
    p = tmp_case(assertions={})
    result = await run_layer_c_trajectory(p)
    assert result.passed, result.reason


@pytest.mark.asyncio
async def test_trajectory_multiple_assertions_all_pass(tmp_case):
    from evals.conversation.runner_layer_c import run_layer_c_trajectory
    p = tmp_case(
        canned_reply="i'll check the weather. kyiv, 18° partly cloudy.",
        simulated_tool_calls=["weather_fetch"],
        assertions={
            "tool_must_be_called": "weather_fetch",
            "text_must_contain": "weather",
            "text_must_not_contain": "Great question",
        },
    )
    result = await run_layer_c_trajectory(p)
    assert result.passed, result.reason


@pytest.mark.asyncio
async def test_trajectory_multiple_assertions_one_fails(tmp_case):
    from evals.conversation.runner_layer_c import run_layer_c_trajectory
    p = tmp_case(
        canned_reply="it's raining.",
        simulated_tool_calls=[],
        assertions={
            "text_must_contain": "raining",
            "tool_must_be_called": "weather_fetch",  # not in simulated_tool_calls
        },
    )
    result = await run_layer_c_trajectory(p)
    assert not result.passed
    assert "tool_must_be_called" in result.reason
