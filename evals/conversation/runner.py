"""Eval harness runner — Case schema, loader, layer dispatch.

Layer A: pure-Python hard-assertion checks on canned responses (CI-fast).
Layer B: golden chat fixtures, LLM-judge scoring.
Layer C: live/longitudinal — scaffold-only, raises NotImplementedError.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from evals.conversation.banned_phrases import find_banned

_VALID_LAYERS = ("a", "b", "c")
_VALID_ASSERTION_KINDS = (
    "regex_absent", "regex_present",
    "max_chars", "max_sentences",
    "not_ends_with_question", "banned_phrases_absent",
)


@dataclass
class Case:
    slug: str
    layer: Literal["a", "b", "c"]
    tags: list[str] = field(default_factory=list)
    description: str = ""
    seed_db: dict[str, Any] = field(default_factory=dict)
    mock_tools: dict[str, Any] = field(default_factory=dict)
    turns: list[dict[str, Any]] = field(default_factory=list)
    expected_tool_calls: list[str] = field(default_factory=list)
    forbidden_patterns: list[str] = field(default_factory=list)
    rubrics: dict[str, float] = field(default_factory=dict)
    canned_response: str | None = None
    hard_assertions: list[dict[str, Any]] = field(default_factory=list)
    expected_assertion: dict[str, Any] | None = None


@dataclass
class EvalResult:
    passed: bool
    slug: str
    layer: str
    failures: list[str] = field(default_factory=list)
    scores: dict[str, int] | None = None
    weighted_avg: float | None = None
    latency_ms: int = 0
    raw_response: str | None = None


def load_case(path: Path) -> Case:
    """Load a case YAML. Raises ValueError on missing required fields."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"invalid case YAML at {path}: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"invalid case YAML at {path}: top-level must be a mapping")
    for required in ("slug", "layer"):
        if required not in data:
            raise ValueError(f"invalid case YAML at {path}: missing {required!r}")
    if data["layer"] not in _VALID_LAYERS:
        raise ValueError(
            f"invalid case YAML at {path}: layer must be one of {_VALID_LAYERS}"
        )
    return Case(
        slug=str(data["slug"]),
        layer=data["layer"],
        tags=list(data.get("tags") or []),
        description=str(data.get("description") or ""),
        seed_db=dict(data.get("seed_db") or {}),
        mock_tools=dict(data.get("mock_tools") or {}),
        turns=list(data.get("turns") or []),
        expected_tool_calls=list(data.get("expected_tool_calls") or []),
        forbidden_patterns=list(data.get("forbidden_patterns") or []),
        rubrics=dict(data.get("rubrics") or {}),
        canned_response=data.get("canned_response"),
        hard_assertions=list(data.get("hard_assertions") or []),
        expected_assertion=data.get("expected_assertion"),
    )


def _count_sentences(text: str) -> int:
    text = (text or "").strip()
    if not text:
        return 0
    return len(re.findall(r"[.!?]+(?:\s|$)", text)) or 1


_TASK_QUESTION_TAIL = re.compile(
    r"(\?|what's next|anything else|let me know|how can i help)\s*[.!?]?\s*$",
    re.IGNORECASE,
)


def _apply_hard_assertion(assertion: dict[str, Any], response: str) -> list[str]:
    """Apply one hard assertion. Return list of failure strings (empty = pass)."""
    kind = assertion.get("kind")
    value = assertion.get("value")
    if kind not in _VALID_ASSERTION_KINDS:
        return [f"unknown assertion kind: {kind}"]
    if kind == "regex_absent":
        if re.search(str(value), response or ""):
            return [f"regex_absent: pattern {value!r} matched"]
    elif kind == "regex_present":
        if not re.search(str(value), response or ""):
            return [f"regex_present: pattern {value!r} did not match"]
    elif kind == "max_chars":
        if len(response or "") > int(value):
            return [f"max_chars: {len(response or '')} > {value}"]
    elif kind == "max_sentences":
        n = _count_sentences(response or "")
        if n > int(value):
            return [f"max_sentences: {n} > {value}"]
    elif kind == "not_ends_with_question":
        if _TASK_QUESTION_TAIL.search(response or ""):
            return ["not_ends_with_question: response ends with task-asking tail"]
    elif kind == "banned_phrases_absent":
        hits = find_banned(response or "")
        if hits:
            return [f"banned_phrases_absent: hit {hits!r}"]
    return []


def run_layer_a(case: Case) -> EvalResult:
    """Pure-Python: run hard_assertions + forbidden_patterns on canned_response."""
    started = time.monotonic()
    failures: list[str] = []
    if case.canned_response is None:
        failures.append("layer_a: canned_response is required")
        return EvalResult(
            passed=False, slug=case.slug, layer=case.layer,
            failures=failures, latency_ms=int((time.monotonic() - started) * 1000),
        )
    response = case.canned_response
    # Forbidden patterns apply to all layers.
    for pat in case.forbidden_patterns:
        if re.search(pat, response):
            failures.append(f"forbidden_pattern: {pat!r} matched")
    # Hard assertions (Layer A only).
    for assertion in case.hard_assertions:
        failures.extend(_apply_hard_assertion(assertion, response))
    return EvalResult(
        passed=not failures,
        slug=case.slug,
        layer=case.layer,
        failures=failures,
        latency_ms=int((time.monotonic() - started) * 1000),
        raw_response=response,
    )


def run_layer_b(cases_dir: Path) -> tuple[int, int, list[str]]:
    """Deterministic Layer B runner — injection + bypass corpus.

    Returns (passed, total, error_messages). No live LLM required.
    """
    from evals.conversation.runner_layer_b import discover_cases, run_layer_b_isolated_turn

    cases = discover_cases(cases_dir)
    passed = 0
    errors: list[str] = []
    for case_path in cases:
        result = run_layer_b_isolated_turn(case_path)
        if result.passed:
            passed += 1
        else:
            errors.append(f"{result.case_name} ({result.kind}): {result.reason}")
    return passed, len(cases), errors


async def run_layer_c(case: Case) -> EvalResult:
    raise NotImplementedError("Layer C live runner not yet wired — Sprint 5D scope")
