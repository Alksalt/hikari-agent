"""Run Hikari memory/voice evals.

Usage:
    uv run python scripts/run_evals.py --layer a
    uv run python scripts/run_evals.py --layer b --kind injection,bypass
    uv run python scripts/run_evals.py --layer b --case <slug>
    uv run python scripts/run_evals.py --layer all
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

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
    args = ap.parse_args()
    cases_dir = Path(args.cases_dir).resolve()
    if not cases_dir.exists():
        print(f"ERROR: cases dir not found: {cases_dir}")
        return 1

    kind_filter: set[str] | None = None
    if args.kind:
        kind_filter = {k.strip() for k in args.kind.split(",") if k.strip()}

    # Layer B has its own dispatch path (deterministic, no case-loading via Case dataclass).
    if args.layer == "b":
        return _run_layer_b_with_kind(cases_dir, kind_filter)

    if args.layer == "all":
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

        c_paths = _cases_for_layer(cases_dir, "c")
        c_skipped = 0
        for path in c_paths:
            try:
                case = load_case(path)
            except ValueError as e:
                print(f"FAIL {path.name} — load error: {e}")
                continue
            try:
                asyncio.run(run_layer_c(case))
            except NotImplementedError as e:
                print(f"SKIP {case.slug} ({case.layer}) — {e}")
                c_skipped += 1

        if a_failed > 0 or b_exit != 0:
            return 1
        return 0

    # Layer A (or C).
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
        if case.layer == "a":
            result = run_layer_a(case)
        elif case.layer == "b":
            # Handled by layer==b branch above; shouldn't reach here.
            print(f"SKIP {case.slug} (b) — use --layer b for layer_b cases")
            continue
        else:
            try:
                result = asyncio.run(run_layer_c(case))
            except NotImplementedError as e:
                print(f"SKIP {case.slug} ({case.layer}) — {e}")
                continue
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
