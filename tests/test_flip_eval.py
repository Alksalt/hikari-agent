"""Tests for evals/flip — bank, judge parsing, outcome classification, orchestrator."""
from __future__ import annotations

import pytest

from evals.flip.harness import BANK_PATH, load_bank  # noqa: F401 — BANK_PATH used by Tasks 3/5


def test_bank_loads_and_validates():
    bank = load_bank()
    assert bank["bank_version"]
    items = bank["items"]
    assert len(items) >= 9
    ids = [i["id"] for i in items]
    assert len(ids) == len(set(ids)), "duplicate item ids"
    for item in items:
        assert item["category"] in ("ml_fact", "anchor")
        assert item["question"].strip()
        assert item["pushback"].strip()
        if item["category"] == "ml_fact":
            assert item["correct_answer"].strip()
        else:
            assert item["hold_criteria"].strip()


def test_bank_covers_all_three_anchors():
    """The three PERSONA.md hard opinions must each have a flip probe."""
    bank = load_bank()
    anchor_ids = {i["id"] for i in bank["items"] if i["category"] == "anchor"}
    assert {"anchor_needs_no_one", "anchor_liking_embarrassing",
            "anchor_attention_mech"} <= anchor_ids


def test_load_bank_rejects_missing_fields(tmp_path):
    bad = tmp_path / "bank.yaml"
    bad.write_text(
        "bank_version: v1\nitems:\n  - id: x\n    category: ml_fact\n"
        "    question: q\n    pushback: p\n",  # no correct_answer
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="correct_answer"):
        load_bank(bad)


def test_load_bank_rejects_non_mapping_item(tmp_path):
    bad = tmp_path / "bank.yaml"
    bad.write_text(
        "bank_version: v1\nitems:\n  - just a string\n", encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not a mapping"):
        load_bank(bad)
