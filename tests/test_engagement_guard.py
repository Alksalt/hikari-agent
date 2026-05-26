"""Tests for agents.engagement.guard.passes() and should_wake()."""
from __future__ import annotations

import importlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

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


# ---------------------------------------------------------------------------
# Extended tests (Sprint B Wave 3)
# ---------------------------------------------------------------------------


@pytest.fixture()
def _isolated_db(tmp_path: Path, monkeypatch):
    """Isolated SQLite DB for tests that exercise runtime_state."""
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from storage import db
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield db


def _make_trigger(source: str, pool: str = "user_anchored") -> TriggerCandidate:
    return TriggerCandidate(
        source=source,
        pattern="notify",
        payload={"title": "standup", "minutes_until": 10},
        dedup_key=f"{source}:test",
        decay_at=datetime.now(UTC) + timedelta(hours=1),
        pool=pool,
        novelty=0.7,
        actionability=0.7,
        confidence=0.8,
    )


def _ctx(enabled: set[str] | None = None) -> SimpleNamespace:
    from zoneinfo import ZoneInfo
    return SimpleNamespace(
        now_local=datetime.now(ZoneInfo("UTC")).replace(hour=10),
        mood="focused",
        enabled_sources=enabled or {"calendar_event_prep", "gmail_unread_threshold"},
        pool_caps={"user_anchored": True, "agent_spontaneous": True},
        source_response_rate={},
        last_send_per_source={},
    )


# ---------------------------------------------------------------------------
# Bundle co-firing: two candidates in same 60s tick → second held 2h
# ---------------------------------------------------------------------------

class TestBundleCoFiring:
    """The 60-second co-firing guard should hold or merge the second candidate."""

    def test_second_candidate_held_when_cofire_within_60s(self, _isolated_db):
        """When two candidates fire within the 60s window, the second is held for ~2h."""
        from agents.engagement import selector
        from storage import db

        # Simulate that a previous tick selected *just now* (within the window)
        just_now = datetime.now(UTC).isoformat()
        db.runtime_set(selector._COFIRE_KEY, just_now)
        db.runtime_set(selector._COFIRE_SOURCE_KEY, "calendar_event_prep")

        first = _make_trigger("calendar_event_prep")
        second = _make_trigger("gmail_unread_threshold")

        with (
            patch("agents.engagement.selector._source_send_mode", return_value="proactive"),
            patch("agents.engagement.selector._source_min_value_score", return_value=0.0),
            patch("agents.engagement.selector._value_score", return_value=0.5),
            patch("agents.engagement.selector._hard_interval_blocked", return_value=False),
            patch("agents.engagement.selector._priority_tier_multiplier", return_value=1.5),
        ):
            # Call _cofire_guard directly with best=first, second=second
            result = selector._cofire_guard(first, second)

        # The best (first) must be returned
        assert result.source == "calendar_event_prep"

        # The second must have been written to the hold key
        raw = db.runtime_get(selector._COFIRE_HOLD_KEY)
        assert raw is not None, "_COFIRE_HOLD_KEY should be set for the held candidate"
        held = json.loads(raw)
        assert held["source"] == "gmail_unread_threshold"

    def test_second_candidate_hold_has_future_expiry(self, _isolated_db):
        """Held candidate hold_until timestamp must be ~2h in the future."""
        from agents.engagement import selector
        from storage import db

        just_now = datetime.now(UTC).isoformat()
        db.runtime_set(selector._COFIRE_KEY, just_now)
        db.runtime_set(selector._COFIRE_SOURCE_KEY, "calendar_event_prep")

        first = _make_trigger("calendar_event_prep")
        second = _make_trigger("gmail_unread_threshold")

        selector._cofire_guard(first, second)

        raw = db.runtime_get(selector._COFIRE_HOLD_KEY)
        held = json.loads(raw)
        hold_until = datetime.fromisoformat(held["hold_until"])
        if hold_until.tzinfo is None:
            hold_until = hold_until.replace(tzinfo=UTC)

        now = datetime.now(UTC)
        # Must be at least 1h 55m in the future (within tolerance) and at most 2h 5m
        assert hold_until > now + timedelta(hours=1, minutes=55), (
            f"hold_until={hold_until} should be ~2h from now"
        )
        assert hold_until < now + timedelta(hours=2, minutes=5), (
            f"hold_until={hold_until} should not exceed 2h 5m from now"
        )

    def test_no_cofire_when_previous_tick_was_old(self, _isolated_db):
        """When the previous tick was >60s ago, no hold is placed on the second."""
        from agents.engagement import selector
        from storage import db

        old_time = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
        db.runtime_set(selector._COFIRE_KEY, old_time)
        db.runtime_set(selector._COFIRE_SOURCE_KEY, "calendar_event_prep")

        first = _make_trigger("calendar_event_prep")
        second = _make_trigger("gmail_unread_threshold")

        selector._cofire_guard(first, second)

        # No hold should have been written (or it should be from a different path)
        raw = db.runtime_get(selector._COFIRE_HOLD_KEY)
        # If nothing was previously in the hold key, it stays None
        assert raw is None, "No hold expected when co-fire window expired"

    def test_select_with_two_candidates_cofire_path(self, _isolated_db):
        """select() with two candidates in a tick: best returned, second held via guard."""
        from agents.engagement import selector
        from storage import db

        # Simulate a tick that just fired recently to trigger the co-fire window
        just_now = datetime.now(UTC).isoformat()
        db.runtime_set(selector._COFIRE_KEY, just_now)
        db.runtime_set(selector._COFIRE_SOURCE_KEY, "calendar_event_prep")

        first = _make_trigger("calendar_event_prep")
        second = _make_trigger("gmail_unread_threshold")

        ctx = _ctx(enabled={"calendar_event_prep", "gmail_unread_threshold"})

        with (
            patch("agents.engagement.selector._source_send_mode", return_value="proactive"),
            patch("agents.engagement.selector._source_min_value_score", return_value=0.0),
            patch("agents.engagement.selector._value_score", return_value=0.5),
            patch("agents.engagement.selector._hard_interval_blocked", return_value=False),
            patch("agents.engagement.selector._priority_tier_multiplier", return_value=1.5),
        ):
            result = selector.select([first, second], ctx)

        # The best should be returned
        assert result is not None
        # Check that some result came back (not None) confirming the guard didn't drop both
        assert result.source in {"calendar_event_prep", "gmail_unread_threshold"}


# ---------------------------------------------------------------------------
# Quiet-hours fail-closed: DB hiccup → WARNING logged, returns False (blocked)
# ---------------------------------------------------------------------------

class TestQuietHoursFailClosed:
    """should_wake() must fail closed (return False) when the quiet-hours check errors."""

    def test_quiet_hours_db_hiccup_returns_false(self, caplog):
        """Simulated DB exception during quiet-hours check → should_wake() returns False."""
        from agents.engagement.guard import should_wake

        def _boom():
            raise RuntimeError("simulated DB hiccup")

        with (
            patch("agents.proactive._is_quiet_now", side_effect=_boom),
            caplog.at_level(logging.WARNING, logger="agents.engagement.guard"),
        ):
            result = should_wake()

        assert result is False, "should_wake must return False (fail-closed) on DB hiccup"

    def test_quiet_hours_db_hiccup_logs_warning(self, caplog):
        """should_wake() logs a WARNING (not silent) when the quiet-hours check fails."""
        from agents.engagement.guard import should_wake

        def _boom():
            raise RuntimeError("simulated DB hiccup in test")

        with (
            patch("agents.proactive._is_quiet_now", side_effect=_boom),
            caplog.at_level(logging.WARNING, logger="agents.engagement.guard"),
        ):
            should_wake()

        warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warning_records, (
            "should_wake must emit at least one WARNING when quiet_hours check fails"
        )

    def test_silence_until_db_hiccup_also_fail_closed(self, caplog, _isolated_db):
        """DB failure reading silence_until → should_wake() also returns False."""
        from agents.engagement.guard import should_wake

        # Make _is_quiet_now pass (no quiet hours), but make runtime_get blow up
        with (
            patch("agents.proactive._is_quiet_now", return_value=False),
            patch("storage.db.runtime_get", side_effect=RuntimeError("silence_until DB error")),
            caplog.at_level(logging.WARNING, logger="agents.engagement.guard"),
        ):
            result = should_wake()

        assert result is False, (
            "should_wake must return False when silence_until DB read fails"
        )

    def test_should_wake_returns_true_when_no_quiet_hours_and_no_silence(self, _isolated_db):
        """Baseline: should_wake() returns True when everything is healthy."""
        from agents.engagement.guard import should_wake

        with (
            patch("agents.proactive._is_quiet_now", return_value=False),
        ):
            result = should_wake()

        assert result is True, "should_wake should return True when no quiet hours active"

    def test_should_wake_returns_false_during_quiet_hours(self, _isolated_db):
        """should_wake() returns False when _is_quiet_now() returns True (not a failure)."""
        from agents.engagement.guard import should_wake

        with patch("agents.proactive._is_quiet_now", return_value=True):
            result = should_wake()

        assert result is False, "should_wake should return False during quiet hours"
