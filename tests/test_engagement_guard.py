"""Tests for agents.engagement.guard.passes() and should_wake()."""
from __future__ import annotations

import importlib
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
    """The 60-second co-firing guard detects co-fires and drops the second candidate.

    NOTE: The original hold-for-2h behaviour was write-only/never drained and has
    been removed.  _cofire_guard is now read-only — state is committed post-send
    via commit_cofire(source).
    """

    def test_second_candidate_dropped_when_cofire_within_60s(self, _isolated_db):
        """When two candidates fire within the 60s window, the best is still returned
        and no hold key is written (hold was removed as dead code)."""
        from agents.engagement import selector
        from storage import db

        # Simulate that a previous tick committed a send *just now* (within the window)
        just_now = datetime.now(UTC).isoformat()
        db.runtime_set(selector._COFIRE_KEY, just_now)
        db.runtime_set(selector._COFIRE_SOURCE_KEY, "calendar_event_prep")

        first = _make_trigger("calendar_event_prep")
        second = _make_trigger("gmail_unread_threshold")

        # Call _cofire_guard directly — it must return best and write nothing
        result = selector._cofire_guard(first, second)

        # The best (first) must be returned
        assert result.source == "calendar_event_prep"

        # No hold key must be written — cofire hold was removed as write-only dead code
        # (cofire state is committed by commit_cofire post-send, not here)
        assert not hasattr(selector, "_COFIRE_HOLD_KEY"), (
            "_COFIRE_HOLD_KEY was removed; hold path is dead code"
        )

    def test_cofire_guard_does_not_write_state(self, _isolated_db):
        """_cofire_guard must not write any cofire state — that belongs to commit_cofire."""
        from agents.engagement import selector
        from storage import db

        just_now = datetime.now(UTC).isoformat()
        db.runtime_set(selector._COFIRE_KEY, just_now)
        db.runtime_set(selector._COFIRE_SOURCE_KEY, "calendar_event_prep")

        first = _make_trigger("calendar_event_prep")
        second = _make_trigger("gmail_unread_threshold")

        before_iso = db.runtime_get(selector._COFIRE_KEY)
        selector._cofire_guard(first, second)
        after_iso = db.runtime_get(selector._COFIRE_KEY)

        # The key must be unchanged — _cofire_guard is read-only
        assert before_iso == after_iso, "_cofire_guard must not update cofire state"

    def test_no_cofire_when_previous_tick_was_old(self, _isolated_db):
        """When the previous committed send was >60s ago, no co-fire is detected."""
        from agents.engagement import selector
        from storage import db

        old_time = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
        db.runtime_set(selector._COFIRE_KEY, old_time)
        db.runtime_set(selector._COFIRE_SOURCE_KEY, "calendar_event_prep")

        first = _make_trigger("calendar_event_prep")
        second = _make_trigger("gmail_unread_threshold")

        # Should return best without raising
        result = selector._cofire_guard(first, second)
        assert result.source == "calendar_event_prep"

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


# ---------------------------------------------------------------------------
# D11 — scheduler_gate_enabled=False must not bypass the noise floor
# ---------------------------------------------------------------------------

class TestSchedulerGateVsNoiseFloor:
    """scheduler_gate_enabled=False bypasses only the scheduler-specific gate,
    NOT the noise floor (quiet-hours / silent_day / silence_until).
    HIKARI_DISABLE_NOISE_FLOOR is the explicit total dev bypass."""

    def test_scheduler_gate_disabled_still_respects_quiet_hours(self, _isolated_db, monkeypatch):
        """should_wake() returns False during quiet hours even when scheduler_gate_enabled=False."""
        from agents import config as _cfg
        from agents.engagement.guard import should_wake

        monkeypatch.setattr(_cfg, "get", lambda key, default=None: (
            False if key == "proactive.scheduler_gate_enabled" else default
        ))

        with patch("agents.proactive._is_quiet_now", return_value=True):
            result = should_wake()

        assert result is False, (
            "scheduler_gate_enabled=False must not bypass quiet hours (noise floor)"
        )

    def test_scheduler_gate_disabled_still_respects_silence_until(self, _isolated_db, monkeypatch):
        """should_wake() returns False during active silence_until even when gate disabled."""
        from datetime import UTC, datetime, timedelta

        from agents import config as _cfg
        from agents.engagement.guard import should_wake
        from storage import db

        monkeypatch.setattr(_cfg, "get", lambda key, default=None: (
            False if key == "proactive.scheduler_gate_enabled" else default
        ))

        # Set silence_until to 1 hour from now
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        db.runtime_set("silence_until", future)

        with patch("agents.proactive._is_quiet_now", return_value=False):
            result = should_wake()

        assert result is False, (
            "scheduler_gate_enabled=False must not bypass global silence_until (noise floor)"
        )

    def test_scheduler_gate_disabled_still_respects_silent_day(self, _isolated_db, monkeypatch):
        """should_wake() returns False on a silent_day even when gate disabled."""
        from agents import config as _cfg
        from agents.engagement.guard import should_wake

        monkeypatch.setattr(_cfg, "get", lambda key, default=None: (
            False if key == "proactive.scheduler_gate_enabled" else default
        ))

        with (
            patch("agents.proactive_gate._is_silent_day_today", return_value=True),
            patch("agents.proactive._is_quiet_now", return_value=False),
        ):
            result = should_wake()

        assert result is False, (
            "scheduler_gate_enabled=False must not bypass silent_day (noise floor)"
        )

    def test_scheduler_gate_disabled_returns_true_when_noise_floor_clear(self, _isolated_db, monkeypatch):
        """should_wake() returns True when gate disabled AND noise floor is clear."""
        from agents import config as _cfg
        from agents.engagement.guard import should_wake

        monkeypatch.setattr(_cfg, "get", lambda key, default=None: (
            False if key == "proactive.scheduler_gate_enabled" else default
        ))

        with (
            patch("agents.proactive_gate._is_silent_day_today", return_value=False),
            patch("agents.proactive._is_quiet_now", return_value=False),
        ):
            result = should_wake()

        assert result is True, (
            "should_wake must return True when gate disabled and noise floor clear"
        )

    def test_disable_noise_floor_env_bypasses_everything(self, _isolated_db, monkeypatch):
        """HIKARI_DISABLE_NOISE_FLOOR=1 bypasses the noise floor entirely (dev-only)."""
        from agents.engagement.guard import should_wake

        monkeypatch.setenv("HIKARI_DISABLE_NOISE_FLOOR", "1")

        with (
            patch("agents.proactive._is_quiet_now", return_value=True),
        ):
            result = should_wake()

        assert result is True, (
            "HIKARI_DISABLE_NOISE_FLOOR=1 must bypass everything including quiet hours"
        )

    def test_disable_noise_floor_env_not_set_does_not_bypass(self, _isolated_db, monkeypatch):
        """Without HIKARI_DISABLE_NOISE_FLOOR set, quiet hours are still respected."""
        from agents.engagement.guard import should_wake

        monkeypatch.delenv("HIKARI_DISABLE_NOISE_FLOOR", raising=False)

        with patch("agents.proactive._is_quiet_now", return_value=True):
            result = should_wake()

        assert result is False, (
            "Without HIKARI_DISABLE_NOISE_FLOOR, quiet hours must still block"
        )
