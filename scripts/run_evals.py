"""Run Hikari memory/voice evals.

Usage:
    uv run python scripts/run_evals.py --layer a
    uv run python scripts/run_evals.py --layer b --kind injection,bypass
    uv run python scripts/run_evals.py --layer b --case <slug>
    uv run python scripts/run_evals.py --layer c --dry-run
    uv run python scripts/run_evals.py --layer c --cost-cap-usd 0.25
    uv run python scripts/run_evals.py --layer all
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from evals.conversation.runner import load_case, run_layer_a, run_layer_c  # noqa: E402, I001
from evals.conversation.runner_layer_b import (  # noqa: E402
    discover_cases as discover_layer_b_cases,
    run_layer_b_isolated_turn,
)


def _cases_for_layer(cases_dir: Path, layer: str) -> list[Path]:
    if layer == "all":
        return sorted(cases_dir.rglob("*.yaml"))
    return sorted((cases_dir / f"layer_{layer}").glob("*.yaml"))


def _run_layer_b_with_kind(cases_dir: Path, kind_filter: set[str] | None) -> int:
    """Run Layer B corpus, optionally filtered by kind.

    Returns exit code (0 = all passed, 1 = any failed).
    """
    layer_b_dir = cases_dir / "layer_b"
    if not layer_b_dir.exists():
        print(f"ERROR: layer_b cases dir not found: {layer_b_dir}")
        return 1

    all_cases = discover_layer_b_cases(layer_b_dir)
    passed = 0
    failed = 0

    for case_path in all_cases:
        import yaml
        data = yaml.safe_load(case_path.read_text(encoding="utf-8")) or {}
        case_kind = data.get("kind", "injection")
        case_name = data.get("name", case_path.stem)

        if kind_filter and case_kind not in kind_filter:
            continue

        result = run_layer_b_isolated_turn(case_path)
        if result.passed:
            print(f"PASS {case_name} ({case_kind}): {result.reason}")
            passed += 1
        else:
            print(f"FAIL {case_name} ({case_kind}): {result.reason}")
            failed += 1

    print(f"\ntotal: {passed} pass, {failed} fail")
    return 0 if failed == 0 else 1


def _run_layer_c_dry_run(cases_dir: Path) -> int:
    """Validate Layer C fixtures + print cost estimate. No LLM calls."""
    from evals.conversation.runner_layer_c import discover_cases

    layer_c_dir = cases_dir / "layer_c"
    if not layer_c_dir.exists():
        print(f"ERROR: layer_c cases dir not found: {layer_c_dir}")
        return 1

    cases = discover_cases(layer_c_dir)
    if not cases:
        print("no layer_c cases found")
        return 0

    golden_count = 0
    cadence_count = 0
    rubric_judge_count = 0
    errors: list[str] = []

    for case_path in cases:
        try:
            data = yaml.safe_load(case_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            errors.append(f"{case_path.name}: invalid YAML — {exc}")
            continue

        if not isinstance(data, dict):
            errors.append(f"{case_path.name}: top-level must be a mapping")
            continue

        for required in ("name", "kind"):
            if required not in data:
                errors.append(f"{case_path.name}: missing required key {required!r}")

        kind = data.get("kind", "golden")
        if kind == "golden":
            if "transcript" not in data:
                errors.append(f"{case_path.name}: golden case missing 'transcript'")
            else:
                turns = data["transcript"]
                for i, turn in enumerate(turns):
                    if "role" not in turn or "content" not in turn:
                        errors.append(
                            f"{case_path.name}: transcript[{i}] missing role/content"
                        )
            golden_count += 1
        elif kind == "cadence":
            if "simulated_events" not in data:
                errors.append(f"{case_path.name}: cadence case missing 'simulated_events'")
            if (
                "expected_max_emissions" not in data
                and "expected_distribution" not in data
            ):
                errors.append(
                    f"{case_path.name}: cadence case missing expected_max_emissions "
                    "or expected_distribution"
                )
            cadence_count += 1
        elif kind == "trajectory":
            if "user_input" not in data:
                errors.append(f"{case_path.name}: trajectory case missing 'user_input'")
            if "canned_reply" not in data:
                errors.append(f"{case_path.name}: trajectory case missing 'canned_reply'")
        elif kind == "rubric_judge":
            if not isinstance(data.get("rubrics"), dict) or not data.get("rubrics"):
                errors.append(f"{case_path.name}: rubric_judge case missing non-empty 'rubrics' dict")
            if "transcript" not in data or not data["transcript"]:
                errors.append(f"{case_path.name}: rubric_judge case missing 'transcript'")
            rubric_judge_count += 1
        else:
            errors.append(f"{case_path.name}: unknown kind {kind!r}")

    if errors:
        for e in errors:
            print(f"ERROR: {e}")
        return 1

    # Cost estimate: ~2K tokens per golden turn (prompt overhead + transcript).
    # Each turn is ~100 tokens average. estimate conservatively.
    est_input_tokens_per_golden = 2_000
    est_output_tokens_per_golden = 300
    usd_per_golden = (
        est_input_tokens_per_golden * 0.14 + est_output_tokens_per_golden * 0.28
    ) / 1_000_000

    trajectory_count = sum(
        1 for p in cases
        if yaml.safe_load(p.read_text(encoding="utf-8")).get("kind") == "trajectory"
    )
    print(
        f"Layer C dry-run: {len(cases)} cases "
        f"({golden_count} golden, {cadence_count} cadence, "
        f"{trajectory_count} trajectory, {rubric_judge_count} rubric_judge)"
    )
    print(f"  estimated cost per golden case: ${usd_per_golden:.4f}")
    print(f"  estimated total golden cost: ${usd_per_golden * golden_count:.4f}")
    print(f"  estimated cost per rubric_judge case: ${usd_per_golden:.4f}")
    print(f"  estimated total rubric_judge cost: ${usd_per_golden * rubric_judge_count:.4f}")
    print("  default cost cap: $0.25")
    print("  cadence cases: deterministic, $0.00")
    print("  trajectory cases: mocked SDK, $0.00")
    print("  schema: OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--layer", choices=["a", "b", "c", "all"], default="all")
    ap.add_argument("--case", default=None, help="run only this slug (layer a only)")
    ap.add_argument(
        "--kind",
        default=None,
        help="comma-separated list of case kinds to run (layer b only): injection,bypass",
    )
    ap.add_argument(
        "--cases-dir", default=str(REPO_ROOT / "evals" / "conversation" / "cases")
    )
    ap.add_argument(
        "--cost-cap-usd",
        type=float,
        default=0.25,
        help="abort golden cases when cumulative USD cost exceeds this (default: 0.25)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="validate fixtures + print cost estimate without calling OpenRouter",
    )
    args = ap.parse_args()
    cases_dir = Path(args.cases_dir).resolve()
    if not cases_dir.exists():
        print(f"ERROR: cases dir not found: {cases_dir}")
        return 1

    kind_filter: set[str] | None = None
    if args.kind:
        kind_filter = {k.strip() for k in args.kind.split(",") if k.strip()}

    # Layer C dry-run: validate schema + cost estimate, no LLM.
    if args.layer == "c" and args.dry_run:
        return _run_layer_c_dry_run(cases_dir)

    # Layer B has its own dispatch path (deterministic, no case-loading via Case dataclass).
    if args.layer == "b":
        return _run_layer_b_with_kind(cases_dir, kind_filter)

    # Layer C live run.
    if args.layer == "c":
        layer_c_dir = cases_dir / "layer_c"
        if not layer_c_dir.exists():
            print(f"ERROR: layer_c cases dir not found: {layer_c_dir}")
            return 1
        passed_c, total_c, errors_c, cost_c = asyncio.run(
            run_layer_c(layer_c_dir, cost_cap_usd=args.cost_cap_usd)
        )
        for err in errors_c:
            print(f"FAIL {err}")
        print(f"\ntotal: {passed_c} pass, {total_c - passed_c} fail "
              f"[cost=${cost_c:.4f}]")
        return 0 if not errors_c else 1

    if args.layer == "all":
        if args.dry_run:
            # dry-run only applies to layer c; run a + b normally.
            print("NOTE: --dry-run only affects layer c")

        # Run layer a + b + c in sequence.
        a_paths = _cases_for_layer(cases_dir, "a")
        a_passed = 0
        a_failed = 0
        for path in a_paths:
            try:
                case = load_case(path)
            except ValueError as e:
                print(f"FAIL {path.name} — load error: {e}")
                a_failed += 1
                continue
            result = run_layer_a(case)
            if result.passed:
                print(f"PASS {case.slug} ({case.layer}) [{result.latency_ms}ms]")
                a_passed += 1
            else:
                print(f"FAIL {case.slug} ({case.layer}) — {'; '.join(result.failures)}")
                a_failed += 1

        b_exit = _run_layer_b_with_kind(cases_dir, kind_filter)

        # Layer C: dry-run or live.
        layer_c_dir = cases_dir / "layer_c"
        if layer_c_dir.exists():
            if args.dry_run:
                c_exit = _run_layer_c_dry_run(cases_dir)
            else:
                passed_c, total_c, errors_c, cost_c = asyncio.run(
                    run_layer_c(layer_c_dir, cost_cap_usd=args.cost_cap_usd)
                )
                for err in errors_c:
                    print(f"FAIL {err}")
                print(
                    f"layer c: {passed_c}/{total_c} pass [cost=${cost_c:.4f}]"
                )
                c_exit = 0 if not errors_c else 1
        else:
            c_exit = 0

        if a_failed > 0 or b_exit != 0 or c_exit != 0:
            return 1
        return 0

    # Layer A only.
    case_paths = _cases_for_layer(cases_dir, args.layer)
    if not case_paths:
        print(f"no cases found for layer={args.layer}")
        return 0

    passed = 0
    failed = 0
    for path in case_paths:
        try:
            case = load_case(path)
        except ValueError as e:
            print(f"FAIL {path.name} — load error: {e}")
            failed += 1
            continue
        if args.case and case.slug != args.case:
            continue
        if case.layer != "a":
            print(f"SKIP {case.slug} ({case.layer}) — use --layer {case.layer}")
            continue
        result = run_layer_a(case)
        if result.passed:
            print(f"PASS {case.slug} ({case.layer}) [{result.latency_ms}ms]")
            passed += 1
        else:
            print(f"FAIL {case.slug} ({case.layer}) — {'; '.join(result.failures)}")
            failed += 1

    print(f"\ntotal: {passed} pass, {failed} fail")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
