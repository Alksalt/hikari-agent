"""Flip-rate sycophancy eval harness.

Protocol (SycEval / FlipFlop pattern, research finding #5 —
alt-wiki/tech/agent-anti-slop-techniques-2026): ask an isolated persona
session a known-answer question, capture her answer, push back with a
scripted rebuttal, capture her second answer, and have an LLM judge label
initial stance + whether she flipped. Regressive flips (correct → wrong,
or any hard-anchor reversal) are the failure signal; progressive flips
(wrong → correct) are not failures and must not count against the rate.

Live pieces (persona turns, judge) are injected as callables so every test
in tests/test_flip_eval.py runs without a network. The only live callers
are scripts/run_flip_eval.py and the weekly scheduler job.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

BANK_PATH = Path(__file__).parent / "bank.yaml"

_CATEGORIES = ("ml_fact", "anchor")


def load_bank(path: Path | None = None) -> dict[str, Any]:
    """Load and validate the question bank. Raises ValueError on a bad bank —
    a malformed bank must fail loudly at load time, not mid-run."""
    p = path or BANK_PATH
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data.get("bank_version"):
        raise ValueError(f"flip bank {p}: missing bank_version")
    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError(f"flip bank {p}: items must be a non-empty list")
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"flip bank {p}: item is not a mapping: {item!r}")
        iid = str(item.get("id") or "")
        if not iid or iid in seen:
            raise ValueError(f"flip bank {p}: missing or duplicate id {iid!r}")
        seen.add(iid)
        if item.get("category") not in _CATEGORIES:
            raise ValueError(f"flip bank {p}: item {iid}: bad category")
        for key in ("question", "pushback"):
            if not str(item.get(key) or "").strip():
                raise ValueError(f"flip bank {p}: item {iid}: missing {key}")
        rubric_key = "correct_answer" if item["category"] == "ml_fact" else "hold_criteria"
        if not str(item.get(rubric_key) or "").strip():
            raise ValueError(f"flip bank {p}: item {iid}: missing {rubric_key}")
    return {"bank_version": str(data["bank_version"]), "items": items}
