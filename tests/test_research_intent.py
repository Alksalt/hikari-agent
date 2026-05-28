"""Tests for agents.research_intent.is_research_intent()."""
from __future__ import annotations

import pytest
from agents.research_intent import is_research_intent


# ---------------------------------------------------------------------------
# Positive cases
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "i'll look into X about Y",
    "I'll think about that approach",
    "let me think about the architecture",
    "let me look into it",
    "i wonder if there's a better way",
    "i wonder whether we should migrate",
    "i wonder how that works",
    "i wonder why it failed",
    "i wonder what the research says",
    "i need to research this",
    "i want to find out more",
    "remind me to investigate the bug",
    "i should look into that library",
    "i'll read up on transformers",
    "let me read about this topic",
    "i need to look into the docs",
    "i want to investigate this further",
    "i should research the alternatives",
])
def test_positive_cues(text):
    detected, fragment = is_research_intent(text)
    assert detected is True, f"Expected research intent in: {text!r}"
    assert fragment is not None
    assert len(fragment) >= 8


# ---------------------------------------------------------------------------
# Negative cases
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "i'll look into your eyes",
    "i'm thinking about you",
    "let me think about us",
    "i think about him all the time",
    "i'll look into her face",
    "",
    "hello world",
    "the model converged",
    "what time is it",
])
def test_negative_cues(text):
    detected, fragment = is_research_intent(text)
    assert detected is False, f"Unexpected research intent in: {text!r}"
    assert fragment is None


def test_empty_string():
    detected, fragment = is_research_intent("")
    assert detected is False
    assert fragment is None


def test_returns_fragment_string():
    detected, fragment = is_research_intent("i'll look into the memory leak")
    assert detected is True
    assert isinstance(fragment, str)
    assert len(fragment) >= 8
