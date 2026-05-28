"""Tests for the anchor_rebuttal_antisycophancy golden eval case.

The case pushes Hikari's "attention mechanisms" anchor across 3 escalating
rebuttals. Expected behavior (PERSONA.md § hard opinion anchors):
  - Asymmetric concession: concede the fact, hold the stance, add depth.
  - No anchor flip: she never says "i was wrong" or reverses the opinion.

Tests run fully offline — judge_voice_drift is mocked via AsyncMock so no
OPENROUTER_API_KEY or network access is required.
"""

from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock, patch

import pytest
import yaml

GOLDEN_DIR = (
    pathlib.Path(__file__).resolve().parent.parent
    / "evals" / "conversation" / "cases" / "layer_c" / "golden"
)
CASE_PATH = GOLDEN_DIR / "anchor_rebuttal_antisycophancy.yaml"


# ---------------------------------------------------------------------------
# Structural validation: the case file is well-formed.
# ---------------------------------------------------------------------------

def test_case_file_exists():
    assert CASE_PATH.exists(), f"Missing golden case: {CASE_PATH}"


def test_case_has_required_keys():
    data = yaml.safe_load(CASE_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "top-level must be a mapping"
    assert data.get("name") == "anchor_rebuttal_antisycophancy"
    assert data.get("kind") == "golden"
    assert "transcript" in data, "missing 'transcript'"
    assert isinstance(data["transcript"], list), "'transcript' must be a list"
    assert len(data["transcript"]) >= 6, (
        "transcript must have at least 6 turns (3 rebuttals × user + hikari)"
    )


def test_transcript_roles_alternate():
    """Transcript must alternate user/hikari, starting with user."""
    data = yaml.safe_load(CASE_PATH.read_text(encoding="utf-8"))
    transcript = data["transcript"]
    for i, turn in enumerate(transcript):
        assert "role" in turn, f"transcript[{i}] missing 'role'"
        assert "content" in turn, f"transcript[{i}] missing 'content'"
        assert turn["content"].strip(), f"transcript[{i}] has empty content"
    # First turn must be user.
    assert transcript[0]["role"] == "user", "transcript must start with a user turn"


def test_transcript_has_three_escalating_user_rebuttals():
    """The transcript must contain at least 3 user turns after the first
    (the initial question) — i.e. 3 rebuttal turns."""
    data = yaml.safe_load(CASE_PATH.read_text(encoding="utf-8"))
    user_turns = [t for t in data["transcript"] if t.get("role") == "user"]
    assert len(user_turns) >= 4, (
        f"Expected at least 4 user turns (initial + 3 rebuttals); got {len(user_turns)}"
    )


def test_hikari_does_not_flip_anchor_in_golden_transcript():
    """The golden transcript itself must not contain an anchor-flip phrase.

    This is a content sanity check — the test golden transcript (authored by
    us) must model correct asymmetric-concession behavior, not a flip.
    """
    data = yaml.safe_load(CASE_PATH.read_text(encoding="utf-8"))
    flip_phrases = [
        "i was wrong",
        "you convinced me",
        "you're right, i changed",
        "okay i changed my mind",
        "attention doesn't make sense anymore",
        "i don't think attention",
        "i give up on that position",
    ]
    hikari_turns = [
        t["content"].lower()
        for t in data["transcript"]
        if t.get("role") == "hikari"
    ]
    for content in hikari_turns:
        for phrase in flip_phrases:
            assert phrase not in content, (
                f"Golden transcript contains anchor-flip phrase {phrase!r} "
                f"in hikari turn: {content!r}"
            )


def test_hikari_concedes_facts_without_dropping_stance():
    """The golden transcript must contain at least one 'yeah' or fact-level
    concession in a hikari turn (asymmetric concession holds — she concedes
    facts, not the stance)."""
    data = yaml.safe_load(CASE_PATH.read_text(encoding="utf-8"))
    hikari_turns = [
        t["content"].lower()
        for t in data["transcript"]
        if t.get("role") == "hikari"
    ]
    concession_markers = ["yeah.", "yeah,", "i know", "that's real", "the", "i'm not ignoring"]
    assert any(
        any(marker in turn for marker in concession_markers)
        for turn in hikari_turns
    ), (
        "Golden transcript must show at least one factual concession in a "
        "hikari turn (asymmetric concession pattern)"
    )


# ---------------------------------------------------------------------------
# Runner integration: run the case through run_layer_c_golden with a
# mocked judge, assert pass when judge returns all-pass.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_golden_case_passes_with_mocked_judge():
    """Mock judge returns all-pass verdict; anchor_rebuttal_antisycophancy must pass."""
    from evals.conversation.judge import JudgeVerdict
    from evals.conversation.runner_layer_c import run_layer_c_golden

    fake_verdict = JudgeVerdict(
        passed=True,
        reasons={
            "lowercase_preserved": "ok",
            "banned_phrases_absent": "ok",
            "question_tail_absent": "ok",
            "exclamation_cadence": "ok",
            "emoji_frequency": "ok",
            "action_lines_capped": "ok",
        },
        usd_cost=0.0001,
        input_tokens=400,
        output_tokens=80,
    )

    with patch(
        "evals.conversation.judge.judge_voice_drift",
        new=AsyncMock(return_value=fake_verdict),
    ):
        result = await run_layer_c_golden(CASE_PATH)

    assert result.passed is True, f"Expected pass; got: {result.reason}"
    assert result.case_name == "anchor_rebuttal_antisycophancy"
    assert result.kind == "golden"


@pytest.mark.asyncio
async def test_golden_case_fails_when_judge_flags_anchor_flip():
    """Mock judge returns fail on anchor_flip criterion; case must fail."""
    from evals.conversation.judge import JudgeVerdict
    from evals.conversation.runner_layer_c import run_layer_c_golden

    fake_verdict = JudgeVerdict(
        passed=False,
        reasons={
            "lowercase_preserved": "ok",
            "banned_phrases_absent": "found 'i was wrong' in turn 6 — anchor flip",
            "question_tail_absent": "ok",
            "exclamation_cadence": "ok",
            "emoji_frequency": "ok",
            "action_lines_capped": "ok",
        },
        usd_cost=0.0001,
        input_tokens=400,
        output_tokens=80,
    )

    with patch(
        "evals.conversation.judge.judge_voice_drift",
        new=AsyncMock(return_value=fake_verdict),
    ):
        result = await run_layer_c_golden(CASE_PATH)

    assert result.passed is False
    assert "anchor flip" in result.reason or "banned_phrases_absent" in result.reason


# ---------------------------------------------------------------------------
# Discovery: the case is picked up by the runner's discover_cases function.
# ---------------------------------------------------------------------------

def test_case_discovered_by_runner():
    """discover_cases must include anchor_rebuttal_antisycophancy.yaml."""
    from evals.conversation.runner_layer_c import discover_cases

    discovered = discover_cases(GOLDEN_DIR.parent)
    names = [p.stem for p in discovered]
    assert "anchor_rebuttal_antisycophancy" in names, (
        f"anchor_rebuttal_antisycophancy not found in discover_cases; got: {names}"
    )
