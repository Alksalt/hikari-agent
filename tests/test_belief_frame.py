"""Stage B-2 belief-frame guard — two-layer regex that flags when the user
asserts a factual claim as their personal belief, so the recall subagent
flips to adversarial mode.

These tests exercise the wiring + the regex correctness, not the full
end-to-end recall flow (covered separately by integration tests).
"""

from __future__ import annotations

import pytest

from agents import belief_frame, config


@pytest.fixture(autouse=True)
def _reset_caches():
    config.reload()
    belief_frame.reload_patterns()
    yield
    config.reload()
    belief_frame.reload_patterns()


# ---------- BELIEF_RE: clear belief assertions should match ----------

@pytest.mark.parametrize(
    "text",
    [
        "i think attention is overrated",
        "i'm pretty sure cold rice is better",
        "i'm sure that's wrong",
        "i believe X is wrong",
        "imo X is mid",
        "in my opinion this paper is hype",
        "i'm convinced the loss curve is broken",
        "i'm fairly certain the eval is bad",
    ],
)
def test_belief_re_matches_clear_assertions(text: str):
    hit, fragment = belief_frame.is_belief_assertion(text)
    assert hit, f"expected belief match on {text!r}"
    assert fragment is not None
    assert fragment.lower() in text.lower()


# ---------- BELIEF_EXCLUSION_RE: casual phrases must NOT match ----------

@pytest.mark.parametrize(
    "text",
    [
        "i think about you",
        "i think of you sometimes",
        "i think back to that",
        "i believe in you",
        "i believe in us",
        "i believe in this",
        "i'm sure you'll be fine",
        "i'm sure we'll be ok",
        "i'm sure they'll be alright",
    ],
)
def test_belief_re_excludes_casual_phrases(text: str):
    hit, fragment = belief_frame.is_belief_assertion(text)
    assert not hit, f"casual phrase {text!r} should NOT trigger belief mode"
    assert fragment is None


# ---------- Belief assertions about correctness of *another party* should match ----------

@pytest.mark.parametrize(
    "text",
    [
        "i think you're wrong about that",
        "i think you're right",
        "i think you're wrong about attention sinks",
        "i believe you're mistaken",
    ],
)
def test_belief_re_does_match_meaningful_belief_about_other(text: str):
    """A belief assertion about someone else's correctness is a factual
    claim and must be flagged for adversarial recall — it is NOT a casual
    'i think about you' phrase."""
    hit, fragment = belief_frame.is_belief_assertion(text)
    assert hit, (
        f"meaningful belief about correctness should match: {text!r}; "
        f"got hit={hit} fragment={fragment!r}"
    )
    assert fragment is not None


def test_compound_message_with_casual_phrase_suppresses_real_belief():
    """Known trade-off (architect's recommendation: conservative is better
    than aggressive). If a message mixes a casual phrase and a real belief
    assertion, the casual phrase's exclusion match suppresses detection of
    the entire message. False positives (Hikari unnecessarily adversarial
    on casual text) are worse than false negatives.

    This test documents the gap explicitly so future contributors know it's
    intentional. If false-negative rate becomes a problem in production,
    refactor to per-match exclusion semantics.
    """
    text = "i think about you and i'm pretty sure the eval is broken"
    hit, fragment = belief_frame.is_belief_assertion(text)
    assert not hit  # full-text exclusion suppresses the entire message
    assert fragment is None


# ---------- empty / whitespace text ----------

def test_belief_re_empty_text_returns_false():
    assert belief_frame.is_belief_assertion("") == (False, None)
    assert belief_frame.is_belief_assertion("   ") == (False, None)


def test_belief_re_unrelated_text_returns_false():
    hit, fragment = belief_frame.is_belief_assertion("hey what's up")
    assert not hit
    assert fragment is None


# ---------- config gate: disabled => always False ----------

def test_belief_disabled_returns_false(tmp_path, monkeypatch):
    cfg_text = (
        "belief_frame:\n"
        "  enabled: false\n"
        '  adversarial_instruction_template: "[adv {matched!r}]"\n'
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()
    belief_frame.reload_patterns()

    hit, fragment = belief_frame.is_belief_assertion("i think attention is overrated")
    assert not hit
    assert fragment is None


# ---------- adversarial prompt suffix renders + carries the fragment ----------

def test_adversarial_prompt_includes_fragment():
    suffix = belief_frame.adversarial_prompt_suffix("i think")
    assert "i think" in suffix
    # The default template carries enough of an adversarial signal that the
    # recall agent will see one of the markers we documented in subagents.py.
    assert "contradict" in suffix.lower() or "adversarial" in suffix.lower()


def test_adversarial_prompt_uses_config_template(tmp_path, monkeypatch):
    cfg_text = (
        "belief_frame:\n"
        "  enabled: true\n"
        '  adversarial_instruction_template: "[CUSTOM marker fragment={matched!r}]"\n'
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()
    belief_frame.reload_patterns()

    suffix = belief_frame.adversarial_prompt_suffix("imo")
    assert "[CUSTOM marker" in suffix
    assert "imo" in suffix


def test_adversarial_prompt_tolerates_template_without_placeholder(tmp_path, monkeypatch):
    """If the user removes the {matched} placeholder, we fall back gracefully
    instead of crashing the bridge."""
    cfg_text = (
        "belief_frame:\n"
        "  enabled: true\n"
        '  adversarial_instruction_template: "[adversarial mode]"\n'
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()
    belief_frame.reload_patterns()

    suffix = belief_frame.adversarial_prompt_suffix("i think")
    assert "[adversarial mode]" in suffix


# ---------- config override of pattern lists ----------

def test_config_override_belief_patterns(tmp_path, monkeypatch):
    cfg_text = (
        "belief_frame:\n"
        "  enabled: true\n"
        '  adversarial_instruction_template: "[adv {matched!r}]"\n'
        "  belief_patterns:\n"
        '    - "(?i)\\\\bbananas?\\\\b"\n'
        "  exclusion_patterns: []\n"
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()
    belief_frame.reload_patterns()

    # Default belief phrasing no longer matches (override replaces).
    hit, _ = belief_frame.is_belief_assertion("i think attention is overrated")
    assert not hit
    # The override DOES match.
    hit2, fragment2 = belief_frame.is_belief_assertion("i love bananas")
    assert hit2
    assert fragment2 is not None
    assert "banana" in fragment2.lower()
