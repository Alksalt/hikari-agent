"""Run Hikari memory/voice evals.

Usage:
    uv run python scripts/run_evals.py --layer a
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

from evals.conversation.runner import load_case, run_layer_a, run_layer_b, run_layer_c  # noqa: E402


def _cases_for_layer(cases_dir: Path, layer: str) -> list[Path]:
    if layer == "all":
        return sorted(cases_dir.rglob("*.yaml"))
    return sorted((cases_dir / f"layer_{layer}").glob("*.yaml"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--layer", choices=["a", "b", "c", "all"], default="all")
    ap.add_argument("--case", default=None, help="run only this slug")
    ap.add_argument(
        "--cases-dir", default=str(REPO_ROOT / "evals" / "conversation" / "cases")
    )
    args = ap.parse_args()
    cases_dir = Path(args.cases_dir).resolve()
    if not cases_dir.exists():
        print(f"ERROR: cases dir not found: {cases_dir}")
        return 1

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
            try:
                result = asyncio.run(run_layer_b(case))
            except NotImplementedError as e:
                print(f"SKIP {case.slug} ({case.layer}) — {e}")
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
