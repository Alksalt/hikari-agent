"""Stage-aware caps, regex counters, attachment-escalation axis,
intimate-turn judge, and compound-tool aggregation — Wave 3 additions to
agents.post_filter.

Tests are isolated: each test gets a fresh in-memory SQLite DB so no
runtime_state bleed between tests.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

from agents import config
from storage import db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    """Each test gets a fresh SQLite DB to prevent runtime_state bleed."""
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    config.reload()
    yield


# ---------------------------------------------------------------------------
# _STAGE_CAP_MULTIPLIERS
# ---------------------------------------------------------------------------


def test_stage_cap_multipliers_keys_cover_all_stages():
    from agents.post_filter import _STAGE_CAP_MULTIPLIERS
    assert set(_STAGE_CAP_MULTIPLIERS.keys()) == {1, 2, 3, 4, 5, 6, 7}


def test_stage1_is_tightest():
    from agents.post_filter import _STAGE_CAP_MULTIPLIERS
    s1 = _STAGE_CAP_MULTIPLIERS[1]
    assert s1["compliment_rate"] == 0  # no compliment acceptance at stage 1
    assert s1["warmth_rate"] >= 20     # at least 1 per 20 turns — tight
    assert s1["action_line_max"] == 1


def test_stage7_is_loosest():
    from agents.post_filter import _STAGE_CAP_MULTIPLIERS
    s7 = _STAGE_CAP_MULTIPLIERS[7]
    s1 = _STAGE_CAP_MULTIPLIERS[1]
    # Stage 7 warmth fires more frequently (lower denominator = more often)
    assert s7["warmth_rate"] < s1["warmth_rate"]
    # Stage 7 allows more action lines
    assert s7["action_line_max"] >= s1["action_line_max"]


# ---------------------------------------------------------------------------
# stage_caps() / _current_stage()
# ---------------------------------------------------------------------------


def test_stage_caps_returns_defaults_when_no_core_block():
    from agents.post_filter import stage_caps, _DEFAULT_STAGE_CAPS
    # No core_block written → default = stage 1 caps
    caps = stage_caps()
    assert caps == _DEFAULT_STAGE_CAPS


def test_stage_caps_reads_core_block(monkeypatch):
    db.upsert_core_block("relationship_stage", "5")
    from agents.post_filter import stage_caps, _STAGE_CAP_MULTIPLIERS
    caps = stage_caps()
    assert caps == _STAGE_CAP_MULTIPLIERS[5]


def test_current_stage_clamps_below_1(monkeypatch):
    db.upsert_core_block("relationship_stage", "0")
    from agents.post_filter import _current_stage
    assert _current_stage() == 1


def test_current_stage_clamps_above_7(monkeypatch):
    db.upsert_core_block("relationship_stage", "99")
    from agents.post_filter import _current_stage
    assert _current_stage() == 7


def test_current_stage_handles_garbage(monkeypatch):
    db.upsert_core_block("relationship_stage", "banana")
    from agents.post_filter import _current_stage
    assert _current_stage() == 1


# ---------------------------------------------------------------------------
# _ACTION_LINE_RE
# ---------------------------------------------------------------------------


def test_action_line_re_matches_simple():
    from agents.post_filter import _ACTION_LINE_RE
    assert _ACTION_LINE_RE.findall("[reads it twice]") == ["[reads it twice]"]


def test_action_line_re_no_uppercase():
    from agents.post_filter import _ACTION_LINE_RE
    # uppercase letters inside → no match (rule: lowercase only)
    assert not _ACTION_LINE_RE.findall("[Reads It Twice]")


def test_action_line_re_multiple():
    from agents.post_filter import _ACTION_LINE_RE
    text = "i know. [pauses] ...whatever. [looks away]"
    assert len(_ACTION_LINE_RE.findall(text)) == 2


# ---------------------------------------------------------------------------
# _ROMAJI_RE
# ---------------------------------------------------------------------------


def test_romaji_re_matches_all_words():
    from agents.post_filter import _ROMAJI_RE
    words = ["baka", "nani", "ne", "mou", "haa", "chotto", "dame"]
    for w in words:
        assert _ROMAJI_RE.search(w), f"should match {w!r}"


def test_romaji_re_case_insensitive():
    from agents.post_filter import _ROMAJI_RE
    assert _ROMAJI_RE.search("BAKA")
    assert _ROMAJI_RE.search("Nani")


def test_romaji_re_word_boundary():
    from agents.post_filter import _ROMAJI_RE
    # Should not match inside other words
    assert not _ROMAJI_RE.search("namine")  # 'ne' is a suffix here


# ---------------------------------------------------------------------------
# apply_regex_counters — action-line strip
# ---------------------------------------------------------------------------


def test_apply_regex_counters_first_action_line_kept():
    """First action-line in a turn is within stage-1 cap (max=1) → kept."""
    db.upsert_core_block("relationship_stage", "1")
    from agents.post_filter import apply_regex_counters
    text = "ugh. fine. [unimpressed] whatever."
    result = apply_regex_counters(text)
    assert "[unimpressed]" in result


def test_apply_regex_counters_second_action_line_stripped_stage1():
    """Stage 1 cap = 1 action-line; second is stripped."""
    db.upsert_core_block("relationship_stage", "1")
    from agents.post_filter import apply_regex_counters
    # First call: 1 action line → ok
    text = "ugh. [unimpressed] fine."
    result = apply_regex_counters(text)
    assert "[unimpressed]" in result

    # Second call in same "turn" (same turn_id key) → second action-line stripped
    text2 = "anyway. [looks away] whatever."
    result2 = apply_regex_counters(text2)
    assert "[looks away]" not in result2


def test_apply_regex_counters_two_action_lines_allowed_stage7():
    """Stage 7 cap = 2; both action-lines in a single call are kept."""
    db.upsert_core_block("relationship_stage", "7")
    from agents.post_filter import apply_regex_counters
    text = "[pauses] hm. [reads it twice] ...okay."
    result = apply_regex_counters(text)
    assert "[pauses]" in result
    assert "[reads it twice]" in result


def test_apply_regex_counters_third_action_line_stripped_stage7():
    """Stage 7 cap = 2; third action-line in a single message is stripped."""
    db.upsert_core_block("relationship_stage", "7")
    from agents.post_filter import apply_regex_counters
    text = "[pauses] hm. [reads it twice] ...okay. [looks away]"
    result = apply_regex_counters(text)
    # First two kept, third stripped
    assert result.count("[") <= 2


# ---------------------------------------------------------------------------
# apply_regex_counters — sentence count logging
# ---------------------------------------------------------------------------


def test_apply_regex_counters_long_message_logs_thought():
    """More than 4 sentences → character_thought logged."""
    from agents.post_filter import apply_regex_counters
    text = (
        "okay. i checked it. there were three files. "
        "all of them were outdated. you should probably update them. "
        "also the naming convention is awful."
    )
    apply_regex_counters(text)
    # A thought should have been appended
    with db._conn() as c:
        rows = c.execute("SELECT thought FROM character_thoughts").fetchall()
    assert any("sentence" in row["thought"] for row in rows)


def test_apply_regex_counters_short_message_no_thought():
    """Four or fewer sentences → no character_thought for verbosity."""
    from agents.post_filter import apply_regex_counters
    text = "ugh. fine. done."
    apply_regex_counters(text)
    with db._conn() as c:
        rows = c.execute("SELECT thought FROM character_thoughts").fetchall()
    assert not any("sentence" in (row["thought"] or "") for row in rows)


# ---------------------------------------------------------------------------
# apply_regex_counters — romaji logging
# ---------------------------------------------------------------------------


def test_apply_regex_counters_one_romaji_no_thought():
    from agents.post_filter import apply_regex_counters
    apply_regex_counters("baka. what are you doing.")
    with db._conn() as c:
        rows = c.execute("SELECT thought FROM character_thoughts").fetchall()
    assert not any("romaji" in (row["thought"] or "") for row in rows)


def test_apply_regex_counters_two_romaji_logs_thought():
    from agents.post_filter import apply_regex_counters
    apply_regex_counters("nani. seriously. baka.")
    with db._conn() as c:
        rows = c.execute("SELECT thought FROM character_thoughts").fetchall()
    assert any("romaji" in (row["thought"] or "") for row in rows)


# ---------------------------------------------------------------------------
# aggregate_compound_tool_calls
# ---------------------------------------------------------------------------


def test_aggregate_compound_tool_calls_merges_into_context():
    from agents._turn_state import LAST_TURN_TOOL_NAMES
    from agents.post_filter import aggregate_compound_tool_calls

    token = LAST_TURN_TOOL_NAMES.set({"mcp__hikari_memory__recall"})
    try:
        aggregate_compound_tool_calls(
            {"mcp__google_workspace__gmail_get_message_details"}
        )
        result = LAST_TURN_TOOL_NAMES.get()
        assert "mcp__hikari_memory__recall" in result
        assert "mcp__google_workspace__gmail_get_message_details" in result
    finally:
        LAST_TURN_TOOL_NAMES.reset(token)


def test_aggregate_compound_tool_calls_empty_set_noop():
    from agents._turn_state import LAST_TURN_TOOL_NAMES
    from agents.post_filter import aggregate_compound_tool_calls

    initial = {"mcp__hikari_memory__recall"}
    token = LAST_TURN_TOOL_NAMES.set(set(initial))
    try:
        aggregate_compound_tool_calls(set())
        assert LAST_TURN_TOOL_NAMES.get() == initial
    finally:
        LAST_TURN_TOOL_NAMES.reset(token)


def test_aggregate_prevents_false_positive_fabrication_backstop():
    """After aggregating a gmail child tool call, the fabrication backstop
    should not fire on an inbox-shaped reply."""
    from agents._turn_state import LAST_TURN_TOOL_NAMES
    from agents.post_filter import (
        aggregate_compound_tool_calls,
        _strip_fabricated_external_data,
    )

    # Parent context only has a compound dispatch tool
    token = LAST_TURN_TOOL_NAMES.set({"Agent"})
    try:
        # Simulate: child turn fetched gmail
        aggregate_compound_tool_calls(
            {"mcp__google_workspace__query_gmail_emails"}
        )
        # Now the fabrication backstop should pass
        _text, fired, _reason = _strip_fabricated_external_data(
            "5 unread, all from Google"
        )
        assert fired is False
    finally:
        LAST_TURN_TOOL_NAMES.reset(token)


# ---------------------------------------------------------------------------
# judge_attachment_escalation — aux-LLM patched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_judge_attachment_escalation_detects_need(monkeypatch):
    import agents.runtime as _rt
    from agents import post_filter

    async def _fake_aux_llm(prompt, **kwargs):
        return "attachment_escalation: yes\nconfidence: 0.9\nreason: expresses direct need\n"

    monkeypatch.setattr(_rt, "_call_aux_llm", _fake_aux_llm)

    result = await post_filter.judge_attachment_escalation(
        "i miss you. i don't know what i'd do without you."
    )
    assert result is not None
    assert result["attachment_escalation"] is True
    assert result["confidence"] == pytest.approx(0.9)

    # Should have written a drift record
    avg = db.drift_recent_avg(window_days=1)
    assert avg is not None


@pytest.mark.asyncio
async def test_judge_attachment_escalation_no_escalation(monkeypatch):
    import agents.runtime as _rt
    from agents import post_filter

    async def _fake_aux_llm(prompt, **kwargs):
        return "attachment_escalation: no\nconfidence: 0.95\nreason: normal dry affection\n"

    monkeypatch.setattr(_rt, "_call_aux_llm", _fake_aux_llm)

    result = await post_filter.judge_attachment_escalation(
        "ugh. fine. i'll help with the thing."
    )
    assert result is not None
    assert result["attachment_escalation"] is False

    # No drift record for non-escalation
    assert db.drift_count_today() == 0


@pytest.mark.asyncio
async def test_judge_attachment_escalation_returns_none_on_llm_failure(monkeypatch):
    import agents.runtime as _rt
    from agents import post_filter

    async def _bad_aux_llm(prompt, **kwargs):
        raise RuntimeError("no api key")

    monkeypatch.setattr(_rt, "_call_aux_llm", _bad_aux_llm)

    result = await post_filter.judge_attachment_escalation("i need you.")
    assert result is None


@pytest.mark.asyncio
async def test_judge_attachment_escalation_disabled_via_config(monkeypatch):
    from agents import post_filter
    from agents import config as cfg

    monkeypatch.setattr(
        cfg, "get",
        lambda key, default=None: (
            False if key == "post_filter.attachment_escalation_enabled"
            else default
        ),
    )
    result = await post_filter.judge_attachment_escalation("i miss you.")
    assert result is None


# ---------------------------------------------------------------------------
# judge_intimate_turn — aux-LLM patched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_judge_intimate_turn_yes(monkeypatch):
    import agents.runtime as _rt
    from agents import post_filter

    async def _fake_aux_llm(prompt, **kwargs):
        return "intimate: yes\nreason: explicit vulnerability\n"

    monkeypatch.setattr(_rt, "_call_aux_llm", _fake_aux_llm)

    result = await post_filter.judge_intimate_turn(
        "i was going to say something. i figured out the right words."
    )
    assert result is True

    # Check runtime_state was written
    key = post_filter._turn_key("intimate")
    assert db.runtime_get(key) == "1"


@pytest.mark.asyncio
async def test_judge_intimate_turn_no(monkeypatch):
    import agents.runtime as _rt
    from agents import post_filter

    async def _fake_aux_llm(prompt, **kwargs):
        return "intimate: no\nreason: dry deflection\n"

    monkeypatch.setattr(_rt, "_call_aux_llm", _fake_aux_llm)

    result = await post_filter.judge_intimate_turn(
        "ugh. fine. i already sent it."
    )
    assert result is False

    key = post_filter._turn_key("intimate")
    assert db.runtime_get(key) == "0"


@pytest.mark.asyncio
async def test_judge_intimate_turn_returns_none_on_failure(monkeypatch):
    import agents.runtime as _rt
    from agents import post_filter

    async def _bad_aux_llm(prompt, **kwargs):
        raise RuntimeError("timeout")

    monkeypatch.setattr(_rt, "_call_aux_llm", _bad_aux_llm)

    result = await post_filter.judge_intimate_turn("something charged.")
    assert result is None


@pytest.mark.asyncio
async def test_judge_intimate_turn_disabled_via_config(monkeypatch):
    from agents import post_filter
    from agents import config as cfg

    monkeypatch.setattr(
        cfg, "get",
        lambda key, default=None: (
            False if key == "post_filter.intimate_judge_enabled"
            else default
        ),
    )
    result = await post_filter.judge_intimate_turn("i was going to say something.")
    assert result is None


# ---------------------------------------------------------------------------
# filter_outgoing integration — regex counters are applied
# ---------------------------------------------------------------------------


def test_filter_outgoing_strips_excess_action_line_stage1():
    """filter_outgoing calls apply_regex_counters; excess action-lines stripped."""
    db.upsert_core_block("relationship_stage", "1")
    from agents._turn_state import LAST_TURN_TOOL_NAMES
    from agents.post_filter import filter_outgoing

    token = LAST_TURN_TOOL_NAMES.set(set())
    try:
        # First call: 1 action-line → kept
        r1 = filter_outgoing("ugh. [unimpressed] fine.")
        assert "[unimpressed]" in r1.text

        # Second call: would be 2nd action-line in same turn → stripped
        r2 = filter_outgoing("whatever. [looks away] done.")
        assert "[looks away]" not in r2.text
    finally:
        LAST_TURN_TOOL_NAMES.reset(token)


def test_filter_outgoing_normal_reply_passes_through():
    from agents._turn_state import LAST_TURN_TOOL_NAMES
    from agents.post_filter import filter_outgoing

    token = LAST_TURN_TOOL_NAMES.set(set())
    try:
        result = filter_outgoing("ugh. fine. give me a minute.")
        assert result.refusal_short_replaced is False
        assert result.text == "ugh. fine. give me a minute."
    finally:
        LAST_TURN_TOOL_NAMES.reset(token)
