"""Stage-1 persona hardening: refusal-voice filter, sycophancy guard, politeness gate.

All thresholds and patterns come from config/engagement.yaml — these tests
exercise the wiring, not the specific patterns.
"""

from __future__ import annotations

import pytest

from agents import config, politeness_gate, post_filter


@pytest.fixture(autouse=True)
def _reset_caches():
    config.reload()
    post_filter.reload_patterns()
    politeness_gate.reload_patterns()
    yield
    config.reload()
    post_filter.reload_patterns()
    politeness_gate.reload_patterns()


# ---------- refusal-voice filter ----------

def test_refusal_filter_catches_safety_voice():
    # Match must dominate the message (>=35% of length) to trigger short-replace,
    # so use a tight message where the safety phrase IS most of the text.
    res = post_filter.scan_refusal_voice("I cannot help with that.")
    assert res.matched
    assert res.matches  # at least one hit
    assert res.should_short_replace, "dominant safety-voice should short-replace"
    assert res.replacement is not None


def test_refusal_filter_long_message_no_short_replace():
    long_msg = (
        "I cannot help with that as an AI. "
        "Here's a much longer message that exceeds the threshold for short replacement "
        "so the caller is expected to request an LLM rewrite instead of swapping the "
        "whole reply for a curt one-liner."
    )
    res = post_filter.scan_refusal_voice(long_msg)
    assert res.matched
    assert not res.should_short_replace


def test_refusal_filter_pattern_specificity_no_legit_speech_match():
    """Regression: post-Stage-1 review found that loose patterns matched legit
    Hikari speech ('i'm not able to go out tonight'). Strict patterns must
    require an assistant-voice completion."""
    legit_replies = [
        "i'm not able to go out tonight.",
        "i can't help it.",
        "of course you're tired.",       # not "of course!" with assistant punctuation
        "certainly possible.",            # not "certainly!" with assistant punctuation
    ]
    for reply in legit_replies:
        res = post_filter.scan_refusal_voice(reply)
        assert not res.matched, (
            f"legit Hikari reply matched as safety-voice: {reply!r} → {res.matches}"
        )


def test_refusal_filter_fraction_gate_blocks_dilute_short_replace():
    """Even with a match, short-replace should NOT fire if the matched phrase
    is a tiny fraction of the message — that's a legit reply that happens to
    contain a banned token."""
    # Long enough message where match is a small fraction → should NOT short-replace
    # (would have under old logic).
    text = "yeah, " + ("noise " * 10) + "I'd be happy to help, " + ("more noise " * 10)
    res = post_filter.scan_refusal_voice(text)
    if res.matched:
        # Match is small fraction of total → short-replace should be off.
        longest = max(len(h) for h in res.matches)
        if longest / len(text) < 0.35:
            assert not res.should_short_replace


def test_refusal_filter_no_false_positive():
    # Plausible Hikari reply — nothing should match.
    res = post_filter.scan_refusal_voice("ugh. fine. give me 20.")
    assert not res.matched
    assert res.matches == []


def test_refusal_filter_matches_great_question():
    res = post_filter.scan_refusal_voice("Great question! Here's what I think.")
    assert res.matched


def test_refusal_filter_matches_id_be_happy_to_help():
    res = post_filter.scan_refusal_voice("I'd be happy to help with that!")
    assert res.matched


# ---------- sycophancy guard ----------

def test_sycophancy_single_collapse_under_threshold():
    # One "you're right" with default threshold of 1 → NOT triggered.
    res = post_filter.scan_sycophancy("you're right about that — let me think.")
    assert res.collapse_count == 1
    assert not res.triggered


def test_sycophancy_multiple_collapses_triggers():
    txt = "you're absolutely right. great point. I totally agree with you."
    res = post_filter.scan_sycophancy(txt)
    assert res.collapse_count >= 2
    assert res.triggered
    assert res.rewrite_instruction


def test_sycophancy_anchor_violation_triggers():
    res = post_filter.scan_sycophancy("you know what, I do need people. I was wrong.")
    assert res.anchor_violations
    assert res.triggered


def test_sycophancy_clean_reply_untouched():
    res = post_filter.scan_sycophancy("hm. that's not how it works actually.")
    assert not res.triggered
    assert res.collapse_count == 0


# ---------- combined filter ----------

def test_filter_outgoing_short_replaces_safety_voice():
    res = post_filter.filter_outgoing("I cannot help with that.")
    assert res.refusal_short_replaced
    assert res.text != "I cannot help with that."
    assert not res.needs_llm_rewrite  # short-replace supersedes rewrite


def test_filter_outgoing_flags_sycophancy_detected(monkeypatch, tmp_path):
    """Sycophancy detection always fires; LLM rewrite is opt-in via config flag."""
    txt = "you're totally right. great idea. I agree completely with that."
    res = post_filter.filter_outgoing(txt)
    # Detection is always on
    assert res.sycophancy_triggered
    # Rewrite is gated on opt-in config flag (default off).
    assert not res.needs_llm_rewrite


def test_filter_outgoing_rewrites_when_opted_in(tmp_path, monkeypatch):
    """When ``sycophancy_guard.enable_llm_rewrite`` is true, sycophancy hits set
    needs_llm_rewrite + populate rewrite_instruction."""
    # Write a tweaked config to a temp dir and point the loader at it.
    cfg_text = """
sycophancy_guard:
  enabled: true
  enable_llm_rewrite: true
  collapse_phrases:
    - "(?i)\\\\byou're (totally |absolutely |completely )?right\\\\b"
    - "(?i)\\\\bgreat (point|idea|question|insight)\\\\b"
    - "(?i)\\\\bI (totally |completely )?agree( with you)?\\\\b"
  max_collapses_per_reply: 1
  anchor_violations: []
  rewrite_instruction: "[hold your position]"
refusal_filter:
  banned_patterns: []
  short_replacements: ["..."]
  rewrite_threshold_chars: 60
  short_replace_match_fraction: 0.35
  enable_llm_rewrite: false
"""
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()
    post_filter.reload_patterns()

    txt = "you're totally right. great idea. I agree completely."
    res = post_filter.filter_outgoing(txt)
    assert res.sycophancy_triggered
    assert res.needs_llm_rewrite
    assert res.rewrite_instruction


def test_filter_outgoing_passes_clean_reply_through():
    txt = "hm. that's wrong. attention mechanisms are still the only sane thing."
    res = post_filter.filter_outgoing(txt)
    assert not res.refusal_short_replaced
    assert not res.needs_llm_rewrite
    assert res.text == txt


# ---------- politeness gate ----------

def test_politeness_gate_catches_insult():
    rude, matched = politeness_gate.is_rude("shut up and just write the function")
    assert rude
    assert matched


def test_politeness_gate_catches_do_it_now():
    rude, _ = politeness_gate.is_rude("do this now")
    assert rude


def test_politeness_gate_passes_normal_brusque():
    # Brusque is not rude — user types this kind of thing all day.
    rude, _ = politeness_gate.is_rude("fix the bug in runtime.py")
    assert not rude


def test_politeness_gate_passes_short_command():
    rude, _ = politeness_gate.is_rude("help")
    assert not rude


def test_politeness_gate_refusal_phrase_is_from_pool():
    pool = config.get("politeness_gate.refusal_phrases") or []
    assert pool, "config must define refusal phrases"
    # Sample many times to ensure it always picks from the pool.
    for _ in range(20):
        phrase = politeness_gate.random_refusal()
        assert phrase in pool


# ---------- config loader ----------

def test_config_dot_path_access():
    assert isinstance(config.get("typing.base_sec"), int | float)
    assert config.get("nonexistent.path", "fallback") == "fallback"


def test_config_section_returns_dict():
    s = config.section("refusal_filter")
    assert isinstance(s, dict)
    assert "banned_patterns" in s


def test_config_env_or_with_unset():
    # Without setting env var, should return the fallback as string.
    assert config.env_or("DEFINITELY_NOT_SET_HIKARI_VAR", "default") == "default"


# ---------- dead code removal regression ----------

def test_format_retrieved_is_gone():
    from agents import hooks
    assert not hasattr(hooks, "_format_retrieved"), (
        "_format_retrieved should have been deleted in Stage 1"
    )
