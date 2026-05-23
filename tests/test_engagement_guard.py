"""Tests for agents.engagement.guard.passes()."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agents.engagement.guard import passes
from agents.engagement.triggers import TriggerCandidate


def _candidate(filename: str = "foo.md", pattern: str = "question") -> TriggerCandidate:
    return TriggerCandidate(
        source="wiki_new_file",
        pattern=pattern,  # type: ignore[arg-type]
        payload={"filename": filename, "folder": "", "h1": "", "mtime": ""},
        dedup_key=f"wiki_new_file:{filename}",
        decay_at=datetime.now(UTC) + timedelta(hours=1),
    )


def test_guard_rejects_generic_opener():
    ok, reason = passes("hey what's up", _candidate())
    assert not ok
    assert reason == "generic_opener"


def test_guard_rejects_missing_anchor():
    # Text doesn't contain "foo.md"
    ok, reason = passes("new page just landed — want me to read it?", _candidate("foo.md"))
    assert not ok
    assert "missing_anchor" in reason


def test_guard_rejects_question_pattern_no_question_mark():
    # Contains filename but doesn't end with "?"
    ok, reason = passes("new page just landed — foo.md. pretty interesting.", _candidate("foo.md"))
    assert not ok
    assert reason == "question_pattern_missing_question_mark"


def test_guard_passes_valid_message():
    text = "new wiki page just landed — 'foo.md'. want me to read it back at you in 3 sentences?"
    ok, reason = passes(text, _candidate("foo.md"))
    assert ok
    assert reason == "ok"


def test_guard_rejects_empty():
    ok, reason = passes("", _candidate())
    assert not ok
    assert reason == "empty"
