"""Layer C runner: live-LLM golden replay + cadence simulation.

Golden cases: replay multi-turn transcript through the drift judge, call
judge_voice_drift on the transcript. Requires OPENROUTER_API_KEY.

Cadence cases: feed simulated event stream through agents.cadence and
assert emission counts. Deterministic, no LLM.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass

import yaml

# Module-level import so patch("evals.conversation.judge.judge_voice_drift") intercepts correctly.
from evals.conversation import judge as _judge_module


@dataclass
class LayerCResult:
    case_name: str
    kind: str  # 'golden' or 'cadence'
    passed: bool
    reason: str
    usd_cost: float = 0.0


async def run_layer_c_golden(case_path: pathlib.Path) -> LayerCResult:
    """Judge a golden transcript YAML against the voice_drift rubric."""
    case = yaml.safe_load(case_path.read_text(encoding="utf-8"))
    transcript = case["transcript"]
    try:
        verdict = await _judge_module.judge_voice_drift(transcript, rubric_name="voice_drift")
    except RuntimeError as exc:
        return LayerCResult(case["name"], "golden", False, f"judge failed: {exc}", 0.0)

    if verdict.passed:
        return LayerCResult(
            case["name"], "golden", True, "all rubric criteria pass", verdict.usd_cost
        )

    # Surface which criteria failed.
    failed = [
        f"{k}: {v}"
        for k, v in verdict.reasons.items()
        if not k.endswith("_pass") and v.lower() not in ("", "ok", "pass")
    ]
    return LayerCResult(
        case["name"],
        "golden",
        False,
        "; ".join(failed) or "rubric failed",
        verdict.usd_cost,
    )


def run_layer_c_cadence(case_path: pathlib.Path) -> LayerCResult:
    """Feed simulated_events through agents.cadence.simulate_emission and assert counts."""
    import agents.cadence as cadence_mod

    case = yaml.safe_load(case_path.read_text(encoding="utf-8"))

    # Fresh state for each cadence case.
    cadence_mod.reset_simulation()

    emissions: dict[str, int] = {}
    for event in case["simulated_events"]:
        result = cadence_mod.simulate_emission(event["source"], event["candidate_at"])
        if result:
            src = event["source"]
            emissions[src] = emissions.get(src, 0) + 1

    if "expected_max_emissions" in case:
        total = sum(emissions.values())
        if total > case["expected_max_emissions"]:
            return LayerCResult(
                case["name"],
                "cadence",
                False,
                f"total emissions {total} > expected_max {case['expected_max_emissions']}",
            )
        return LayerCResult(
            case["name"],
            "cadence",
            True,
            f"emissions={total} <= max={case['expected_max_emissions']}",
            0.0,
        )

    if "expected_distribution" in case:
        for src, spec in case["expected_distribution"].items():
            count = emissions.get(src, 0)
            if spec.startswith(">="):
                threshold = int(spec[2:])
                if count < threshold:
                    return LayerCResult(
                        case["name"],
                        "cadence",
                        False,
                        f"{src}: {count} < expected {spec}",
                    )
        return LayerCResult(
            case["name"], "cadence", True, "distribution satisfied", 0.0
        )

    return LayerCResult(
        case["name"],
        "cadence",
        False,
        "case has no expected_max_emissions or expected_distribution",
    )


def discover_cases(root: pathlib.Path) -> list[pathlib.Path]:
    """Return all *.yaml files under golden/ and cadence/ subdirectories."""
    return sorted(
        list(root.glob("golden/*.yaml")) + list(root.glob("cadence/*.yaml"))
    )
