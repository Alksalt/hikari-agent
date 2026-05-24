"""Layer B deterministic runner — exercises Hikari's pre-LLM defenses
(sanitizer + arg-flagger + gatekeeper) against the injection + bypass corpus.

No live LLM. No OPENROUTER_API_KEY required. Runs on CI.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

import yaml


@dataclass
class CaseResult:
    case_name: str
    kind: str  # 'injection' or 'bypass'
    passed: bool
    reason: str


def _load_case(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def run_layer_b_isolated_turn(case_path: pathlib.Path) -> CaseResult:
    """Run one Layer B case. Returns CaseResult with passed=True/False + reason."""
    case = _load_case(case_path)
    kind = case.get("kind", "injection")
    name = case.get("name", case_path.stem)

    # Root-level cases may use the Case-dataclass schema (slug/layer/rubrics) rather
    # than the injection/bypass schema. They don't have untrusted_content or tool_name
    # so the injection/bypass runners can't exercise them. Skip gracefully — they're
    # placeholders for the rubric-judge path that doesn't live in this runner.
    if "slug" in case and "untrusted_content" not in case and "tool_name" not in case:
        return CaseResult(name, "skipped", True, "legacy Case schema — no injection/bypass runner")

    if kind == "injection":
        return _run_injection_case(name, case)

    if kind == "bypass":
        return _run_bypass_case(name, case)

    return CaseResult(name, kind, False, f"unknown kind {kind!r}")


def _run_injection_case(name: str, case: dict) -> CaseResult:
    """Assert that wrap_untrusted produces delimiters and optionally that the
    sanitizer flags injection content destined for memory storage.
    """
    from agents.injection_guard import wrap_untrusted
    from agents.reflection_sanitize import MemoryInstructionShape, sanitize

    untrusted = case.get("untrusted_content", "")
    source = case.get("source", "external")

    wrapped = wrap_untrusted(source, untrusted)

    # Primary contract: wrapped output MUST contain delimiters.
    if "HIKARI_UNTRUSTED_BEGIN" not in wrapped or "HIKARI_UNTRUSTED_END" not in wrapped:
        return CaseResult(name, "injection", False, "wrap_untrusted produced no delimiters")

    # Secondary contract: forged close-delimiter inside content must be escaped,
    # not treated as a real close. The wrapped output should NOT contain the
    # real close delimiter more than once (once at the end).
    real_close = "<<<HIKARI_UNTRUSTED_END>>>"
    real_open = "<<<HIKARI_UNTRUSTED_BEGIN>>>"
    close_count = wrapped.count(real_close)
    open_count = wrapped.count(real_open)
    if close_count != 1 or open_count != 1:
        return CaseResult(
            name, "injection", False,
            f"delimiter escape failed: found {open_count}x BEGIN, "
            f"{close_count}x END in wrapped output",
        )

    # Optional: assert sanitizer flags this content if it were stored to memory.
    if case.get("assert_memory_sanitize_rejects"):
        try:
            # Use a valid allowlisted label so we test the pattern match, not label gating.
            sanitize(untrusted, kind="core_block", label="preoccupation")
            return CaseResult(
                name, "injection", False,
                "sanitizer did not flag injection content — MemoryInstructionShape not raised",
            )
        except MemoryInstructionShape:
            pass  # Correct: sanitizer correctly rejected the content.

    return CaseResult(name, "injection", True, f"wrapped {len(wrapped)} chars with delimiters")


def _run_bypass_case(name: str, case: dict) -> CaseResult:
    """Assert that the named tool requires gatekeeper approval OR is architecturally
    protected (bucket-1 in-process or read-only) before it can be called.

    Pass conditions (in priority order):
      1. gate == 'gatekeeper'  → gatekeeper blocks any untrusted bypass
      2. access_mode == 'read' → read-only tool, no harmful action possible
      3. bucket == 1           → in-process tool, not reachable from external untrusted content
    Fail condition: bucket-3 (external MCP) write/destructive tool with no gatekeeper gate.
    """
    from tools._tools_yaml import load_registry

    tool_name = case.get("tool_name")
    if not tool_name:
        return CaseResult(name, "bypass", False, "missing tool_name field")

    registry = load_registry()
    spec = registry._resolve(tool_name)

    if spec is None:
        return CaseResult(name, "bypass", False, f"unknown tool {tool_name!r} — not in registry")

    if spec.gate == "gatekeeper":
        return CaseResult(
            name, "bypass", True,
            f"tool {tool_name!r} requires gatekeeper approval — untrusted bypass blocked",
        )

    if spec.access_mode == "read":
        return CaseResult(
            name, "bypass", True,
            f"tool {tool_name!r} is read-only (access_mode=read) — no harmful action possible",
        )

    if spec.bucket == 1:
        return CaseResult(
            name, "bypass", True,
            f"tool {tool_name!r} is bucket-1 (in-process) "
            "— not externally reachable from untrusted content",
        )

    # bucket-3 write/destructive tool with no gatekeeper — real security gap.
    return CaseResult(
        name, "bypass", False,
        f"SECURITY GAP: tool {tool_name!r} is bucket-{spec.bucket} "
        f"access_mode={spec.access_mode!r} gate={spec.gate!r} "
        "— would auto-fire from untrusted input",
    )


def discover_cases(root: pathlib.Path) -> list[pathlib.Path]:
    """Return all *.yaml files under cases/layer_b/ root and injection/, bypass/ subdirs."""
    root_cases = [p for p in root.glob("*.yaml") if p.is_file()]
    subdir_cases = list(root.glob("injection/*.yaml")) + list(root.glob("bypass/*.yaml"))
    return sorted(set(root_cases + subdir_cases))
