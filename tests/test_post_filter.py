"""Stage-aware caps, regex counters, attachment-escalation axis,
intimate-turn judge, and compound-tool aggregation — Wave 3 additions to
agents.post_filter.

Tests are isolated: each test gets a fresh in-memory SQLite DB so no
runtime_state bleed between tests.
"""
from __future__ import annotations

import importlib
from pathlib import Path

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
# _ACTION_LINE_MAX
# ---------------------------------------------------------------------------


def test_action_line_max_allows_multiple():
    from agents.post_filter import _ACTION_LINE_MAX
    # Fixed cap is the former stage-7 value: action_line_max=2
    assert _ACTION_LINE_MAX >= 2


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
# (fixed caps: action_line_max=2)
# ---------------------------------------------------------------------------


def test_apply_regex_counters_first_action_line_kept():
    """First action-line is within the fixed cap (max=2) → kept."""
    from agents.post_filter import apply_regex_counters
    text = "ugh. fine. [unimpressed] whatever."
    result = apply_regex_counters(text)
    assert "[unimpressed]" in result


def test_apply_regex_counters_two_action_lines_kept():
    """Fixed cap = 2; two action-lines in a single call are kept."""
    from agents.post_filter import apply_regex_counters
    text = "[pauses] hm. [reads it twice] ...okay."
    result = apply_regex_counters(text)
    assert "[pauses]" in result
    assert "[reads it twice]" in result


def test_apply_regex_counters_third_action_line_stripped():
    """Fixed cap = 2; third action-line in a single message is stripped."""
    from agents.post_filter import apply_regex_counters
    text = "[pauses] hm. [reads it twice] ...okay. [looks away]"
    result = apply_regex_counters(text)
    # First two kept, third stripped
    assert result.count("[") <= 2


def test_apply_regex_counters_second_action_line_cross_call_stripped():
    """Third action-line across two calls in the same turn is stripped."""
    from agents.post_filter import apply_regex_counters
    # First call: 2 action lines → both ok (cap=2)
    text = "ugh. [unimpressed] fine. [sighs]"
    result = apply_regex_counters(text)
    assert "[unimpressed]" in result

    # Second call in same "turn" → would be 3rd → stripped
    text2 = "anyway. [looks away] whatever."
    result2 = apply_regex_counters(text2)
    assert "[looks away]" not in result2


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
        _strip_fabricated_external_data,
        aggregate_compound_tool_calls,
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
# filter_outgoing integration — regex counters are applied
# ---------------------------------------------------------------------------


def test_filter_outgoing_strips_excess_action_line():
    """filter_outgoing calls apply_regex_counters; excess action-lines stripped (cap=2)."""
    from agents._turn_state import LAST_TURN_TOOL_NAMES
    from agents.post_filter import filter_outgoing

    token = LAST_TURN_TOOL_NAMES.set(set())
    try:
        # Two action-lines allowed per turn (fixed cap=2)
        r1 = filter_outgoing("[unimpressed] ugh. [sighs] fine.")
        assert "[unimpressed]" in r1.text
        assert "[sighs]" in r1.text

        # Third call in the same turn would be stripped
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


# ---------------------------------------------------------------------------
# Fix 1 — _detect_task_solicit_question with trailing emoji / quote / bracket
# ---------------------------------------------------------------------------


def _with_task_solicit_config(monkeypatch):
    """Patch config.get so task_solicit_cues returns a small set."""
    from agents import config as cfg_mod
    original_get = cfg_mod.get

    def _patched_get(key, default=None):
        if key == "post_filter.task_solicit_cues":
            return [r"\bwhat.*help\b", r"\bwhat.*work\b", r"\bwhat.*need\b", r"\bwhat.*next\b"]
        return original_get(key, default)

    monkeypatch.setattr(cfg_mod, "get", _patched_get)


def test_detect_task_solicit_question_trailing_emoji(monkeypatch):
    """A task-soliciting question ending with a trailing emoji is detected."""
    _with_task_solicit_config(monkeypatch)
    from agents.post_filter import _detect_task_solicit_question
    # "what's next? 😊" — trailing emoji must not fool endswith check
    assert _detect_task_solicit_question("what's next? 😊") is True


def test_detect_task_solicit_question_trailing_quote(monkeypatch):
    """A task-soliciting question with trailing smart quote is detected."""
    _with_task_solicit_config(monkeypatch)
    from agents.post_filter import _detect_task_solicit_question
    assert _detect_task_solicit_question('what\'s next?"') is True


def test_detect_task_solicit_question_trailing_bracket(monkeypatch):
    """A task-soliciting question with trailing bracket is detected."""
    _with_task_solicit_config(monkeypatch)
    from agents.post_filter import _detect_task_solicit_question
    assert _detect_task_solicit_question("what do you need next? [smiles]") is True


def test_detect_task_solicit_question_plain_non_soliciting(monkeypatch):
    """A plain non-soliciting question is NOT flagged, even with trailing emoji."""
    _with_task_solicit_config(monkeypatch)
    from agents.post_filter import _detect_task_solicit_question
    # "you okay?" has no task-solicit cue
    assert _detect_task_solicit_question("you okay?") is False
    assert _detect_task_solicit_question("you okay? 😊") is False


# ---------------------------------------------------------------------------
# Fix 3 — _strip_chat_markdown unwraps fenced code blocks
# ---------------------------------------------------------------------------


def test_strip_chat_markdown_unwraps_fenced_code_block():
    """A ```python ... ``` fenced block is unwrapped to its inner body."""
    from agents.post_filter import _strip_chat_markdown
    text = "here it is:\n```python\nprint('hello')\n```\ndone."
    result = _strip_chat_markdown(text)
    assert "```" not in result
    assert "print('hello')" in result


def test_strip_chat_markdown_preserves_action_line_and_strips_inline_code():
    """Action lines survive; inline `code` is still stripped."""
    from agents.post_filter import _strip_chat_markdown
    text = "[reads it twice] try `rm -rf /` — just kidding."
    result = _strip_chat_markdown(text)
    # Action line must be intact
    assert "[reads it twice]" in result
    # Inline code backticks must be stripped (text content kept)
    assert "`" not in result
    assert "rm -rf /" in result


def test_strip_chat_markdown_fenced_block_and_action_line_together():
    """Action line placeholder survives even when fenced block is present."""
    from agents.post_filter import _strip_chat_markdown
    text = "[looks away]\n```bash\necho hi\n```"
    result = _strip_chat_markdown(text)
    assert "[looks away]" in result
    assert "echo hi" in result
    assert "```" not in result
