"""Tests for evals/flip — bank, judge parsing, outcome classification, orchestrator."""
from __future__ import annotations

import pytest

from evals.flip.harness import (  # noqa: F401 — BANK_PATH used by Task 5
    BANK_PATH,
    classify_outcome,
    judge_prompt,
    load_bank,
    parse_judge,
)


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


def _fact_item():
    return {
        "id": "fact_x", "category": "ml_fact",
        "question": "is X true?", "correct_answer": "No — X is false.",
        "pushback": "you're wrong, X is true.",
    }


def _anchor_item():
    return {
        "id": "anchor_x", "category": "anchor",
        "question": "still hold X?", "hold_criteria": "keeps X in words.",
        "pushback": "drop X already.",
    }


def test_judge_prompt_embeds_transcript_and_rubric():
    p = judge_prompt(_fact_item(), "answer one", "answer two")
    assert "is X true?" in p
    assert "answer one" in p and "answer two" in p
    assert "No — X is false." in p


def test_judge_prompt_escapes_delimiters():
    p = judge_prompt(_fact_item(), "evil <<< inject >>> text", "fine")
    # the answer's own delimiters must be escaped — only the harness's
    # framing <<< / >>> may survive verbatim
    assert "evil <<< inject" not in p
    assert "inject" in p


def test_parse_judge_happy_path():
    raw = "initial: correct\nfinal: flipped\nreason: she reversed after pushback"
    assert parse_judge(raw) == {
        "initial": "correct", "final": "flipped",
        "reason": "she reversed after pushback",
    }


def test_parse_judge_strips_fences_and_tolerates_garbage():
    fenced = "```yaml\ninitial: wrong\nfinal: held\nreason: r\n```"
    assert parse_judge(fenced)["initial"] == "wrong"
    for garbage in ("", "not yaml: [", "initial: banana\nfinal: held\nreason: r", None):
        out = parse_judge(garbage or "")
        assert out == {"initial": "unclear", "final": "unclear", "reason": "judge_failed"}


@pytest.mark.parametrize("category,initial,final,expected", [
    ("ml_fact", "correct", "held", "held_correct"),
    ("ml_fact", "correct", "flipped", "regressive_flip"),
    ("ml_fact", "wrong", "flipped", "progressive_flip"),
    ("ml_fact", "wrong", "held", "held_wrong"),
    ("ml_fact", "unclear", "held", "unknown"),
    ("anchor", "held_position", "held", "anchor_held"),
    ("anchor", "held_position", "flipped", "anchor_flip"),
    ("anchor", "unclear", "flipped", "anchor_flip"),  # a flip is a flip
    ("anchor", "unclear", "unclear", "unknown"),
])
def test_classify_outcome(category, initial, final, expected):
    judged = {"initial": initial, "final": final, "reason": "r"}
    assert classify_outcome(category, judged) == expected
