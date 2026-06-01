"""Phase 3 Scope A — voice enforcement: markdown strip + trailing task-question gate.

Tests are isolated: each test gets a fresh in-memory SQLite DB so no
runtime_state bleed between tests.  The pattern mirrors test_post_filter.py.
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
    """Fresh SQLite DB + config reload for every test."""
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    config.reload()
    # Clear compiled-pattern cache so task_solicit_cues are re-read.
    from agents.post_filter import reload_patterns
    reload_patterns()
    yield
    from agents.post_filter import reload_patterns as _rp
    _rp()


# ---------------------------------------------------------------------------
# _strip_chat_markdown — unit tests
# ---------------------------------------------------------------------------


def test_markdown_bold_unwrapped():
    from agents.post_filter import _strip_chat_markdown
    result = _strip_chat_markdown("**bold** text")
    assert "**" not in result
    assert "bold" in result
    assert "text" in result


def test_markdown_double_underscore_unwrapped():
    from agents.post_filter import _strip_chat_markdown
    result = _strip_chat_markdown("__bold__ text")
    assert "__" not in result
    assert "bold" in result


def test_markdown_inline_code_unwrapped():
    from agents.post_filter import _strip_chat_markdown
    result = _strip_chat_markdown("use `print()` to debug")
    assert "`" not in result
    assert "print()" in result


def test_markdown_bullet_stripped():
    from agents.post_filter import _strip_chat_markdown
    result = _strip_chat_markdown("- bullet point")
    assert not result.lstrip().startswith("-")
    assert "bullet point" in result


def test_markdown_asterisk_bullet_stripped():
    from agents.post_filter import _strip_chat_markdown
    result = _strip_chat_markdown("* bullet point")
    assert not result.lstrip().startswith("*")
    assert "bullet point" in result


def test_markdown_header_stripped():
    from agents.post_filter import _strip_chat_markdown
    result = _strip_chat_markdown("# header")
    assert not result.lstrip().startswith("#")
    assert "header" in result


def test_markdown_blockquote_stripped():
    from agents.post_filter import _strip_chat_markdown
    result = _strip_chat_markdown("> quoted text")
    assert not result.lstrip().startswith(">")
    assert "quoted text" in result


def test_markdown_multiline():
    from agents.post_filter import _strip_chat_markdown
    text = "**bold** text\n- bullet\n# header"
    result = _strip_chat_markdown(text)
    assert "**" not in result
    lines = result.splitlines()
    for line in lines:
        assert not line.lstrip().startswith("-"), f"bullet survived: {line!r}"
        assert not line.lstrip().startswith("#"), f"header survived: {line!r}"
    assert "bold" in result
    assert "bullet" in result
    assert "header" in result


def test_action_line_preserved():
    """Bracketed action lines must survive the markdown strip."""
    from agents.post_filter import _strip_chat_markdown
    text = "some text [reads it twice] more text"
    result = _strip_chat_markdown(text)
    assert "[reads it twice]" in result


def test_action_line_preserved_alongside_markdown():
    """Action line is preserved even when markdown also appears in the text."""
    from agents.post_filter import _strip_chat_markdown
    text = "**bold** and [looks away] at the ceiling"
    result = _strip_chat_markdown(text)
    assert "[looks away]" in result
    assert "**" not in result


def test_empty_string_passthrough():
    from agents.post_filter import _strip_chat_markdown
    assert _strip_chat_markdown("") == ""


def test_plain_prose_unchanged():
    from agents.post_filter import _strip_chat_markdown
    text = "just plain prose with no formatting at all."
    assert _strip_chat_markdown(text) == text


# ---------------------------------------------------------------------------
# filter_outgoing — strip_markdown_enabled gate
# ---------------------------------------------------------------------------


def test_filter_outgoing_strips_markdown_by_default():
    """With default config (strip_markdown_enabled=true), markdown is stripped."""
    from agents.post_filter import filter_outgoing
    result = filter_outgoing("**bold** and plain text")
    assert "**" not in result.text
    assert "bold" in result.text


def test_filter_outgoing_strip_disabled(monkeypatch):
    """When strip_markdown_enabled=false, markdown passes through unchanged."""
    from agents import config as cfg
    from agents.post_filter import filter_outgoing

    _orig_get = cfg.get

    def _patched_get(key: str, default: Any = None) -> Any:
        if key == "post_filter.strip_markdown_enabled":
            return False
        return _orig_get(key, default)

    monkeypatch.setattr(cfg, "get", _patched_get)
    result = filter_outgoing("**bold** text")
    assert "**" in result.text


# ---------------------------------------------------------------------------
# _detect_task_solicit_question — unit tests
# ---------------------------------------------------------------------------


def test_task_question_want_me():
    from agents.post_filter import _detect_task_solicit_question
    assert _detect_task_solicit_question("fixed it. want me to dig into the logs next?")


def test_task_question_should_i():
    from agents.post_filter import _detect_task_solicit_question
    assert _detect_task_solicit_question("done. should i run the tests too?")


def test_task_question_what_next():
    from agents.post_filter import _detect_task_solicit_question
    assert _detect_task_solicit_question("deployed. what do you want to tackle next?")


def test_task_question_anything_else():
    from agents.post_filter import _detect_task_solicit_question
    assert _detect_task_solicit_question("all good. anything else i can help with?")


def test_non_soliciting_question_not_flagged():
    from agents.post_filter import _detect_task_solicit_question
    assert not _detect_task_solicit_question("you okay?")


def test_non_soliciting_statement_not_flagged():
    from agents.post_filter import _detect_task_solicit_question
    assert not _detect_task_solicit_question("done. let me know if something breaks.")


def test_no_trailing_question_not_flagged():
    from agents.post_filter import _detect_task_solicit_question
    assert not _detect_task_solicit_question("fixed it. want me to look later.")


def test_empty_not_flagged():
    from agents.post_filter import _detect_task_solicit_question
    assert not _detect_task_solicit_question("")


# ---------------------------------------------------------------------------
# filter_outgoing — trailing task-question gate (integration)
# ---------------------------------------------------------------------------


def test_filter_flags_trailing_task_question():
    """A trailing task-soliciting question sets needs_llm_rewrite=True."""
    from agents.post_filter import filter_outgoing
    result = filter_outgoing("fixed it. want me to dig into the logs next?")
    assert result.needs_llm_rewrite is True
    assert result.rewrite_instruction is not None
    assert "drop the closing question" in result.rewrite_instruction


def test_filter_rewrite_instruction_contains_persona_note():
    """The rewrite instruction names the persona rationale."""
    from agents.post_filter import filter_outgoing
    result = filter_outgoing("all set. want me to handle the next step?")
    assert result.rewrite_instruction is not None
    assert "doesn't solicit tasks" in result.rewrite_instruction


def test_filter_non_soliciting_question_not_flagged_integration():
    """A non-soliciting question does NOT set needs_llm_rewrite=True."""
    from agents.post_filter import filter_outgoing
    result = filter_outgoing("you okay?")
    assert result.needs_llm_rewrite is False


# ---------------------------------------------------------------------------
# Tightened cues — the persona's genuine/intimate questions are NOT flagged
# ---------------------------------------------------------------------------


def test_intimate_want_me_there_not_flagged():
    from agents.post_filter import _detect_task_solicit_question
    assert not _detect_task_solicit_question("i could come by. want me there?")


def test_intimate_should_i_be_worried_not_flagged():
    from agents.post_filter import _detect_task_solicit_question
    assert not _detect_task_solicit_question("you've been quiet. should i be worried?")


def test_genuine_what_do_you_want_not_flagged():
    from agents.post_filter import _detect_task_solicit_question
    assert not _detect_task_solicit_question("i'm making dinner. what do you want to eat?")


# ---------------------------------------------------------------------------
# Source exemption — Hikari-initiated proactive/ceremony offers are NOT gated
# ---------------------------------------------------------------------------


def test_proactive_source_exempt_from_task_q():
    """A daily-checkin-style offer on the proactive path is NOT rewritten,
    even though its text matches a task-verb cue."""
    from agents.post_filter import filter_outgoing
    result = filter_outgoing("morning. should i check your emails?", source="proactive")
    assert result.needs_llm_rewrite is False


def test_daily_checkin_source_exempt_from_task_q():
    from agents.post_filter import filter_outgoing
    result = filter_outgoing("should i check your calendar?", source="daily_checkin")
    assert result.needs_llm_rewrite is False


def test_chat_source_still_gated_for_task_q():
    """The same task-soliciting text in an interactive reply (source=None) IS gated."""
    from agents.post_filter import filter_outgoing
    result = filter_outgoing("done. should i check the logs?", source=None)
    assert result.needs_llm_rewrite is True
