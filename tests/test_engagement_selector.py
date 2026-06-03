"""Tests for agents.engagement.selector — Wave 2 extensions.

Coverage:
  - send_mode == "silent" source filtered out
  - value_score < min_value_score filters candidate
  - _value_score rubric returns sensible range
  - co-fire guard writes hold for second candidate within 60s
  - on_reaction updates proactive_source_scores EMA and counters
  - _hard_interval_blocked on reengage_silence path
"""
from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agents.engagement.triggers import TriggerCandidate

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from storage import db
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield


def _candidate(
    source: str = "wiki_new_file",
    pool: str = "user_anchored",
    pattern: str = "notify",
    novelty: float = 0.7,
    actionability: float = 0.6,
    confidence: float = 0.8,
    payload: dict | None = None,
) -> TriggerCandidate:
    return TriggerCandidate(
        source=source,
        pool=pool,
        pattern=pattern,
        payload=payload or {"filename": "test.md"},
        dedup_key=f"{source}:test",
        decay_at=datetime.now(UTC) + timedelta(hours=1),
        novelty=novelty,
        actionability=actionability,
        confidence=confidence,
    )


def _make_ctx(
    enabled: set[str] | None = None,
    pool_caps: dict[str, bool] | None = None,
    mood: str = "focused",
    last_send: dict[str, str] | None = None,
) -> SimpleNamespace:
    from zoneinfo import ZoneInfo
    return SimpleNamespace(
        now_local=datetime.now(ZoneInfo("UTC")),
        mood=mood,
        enabled_sources=enabled or {"wiki_new_file", "gmail_unread_threshold", "calendar_event_prep"},
        pool_caps=pool_caps or {"user_anchored": True, "agent_spontaneous": True},
        source_response_rate={},
        last_send_per_source=last_send or {},
    )


# ---------------------------------------------------------------------------
# send_mode filter
# ---------------------------------------------------------------------------

class TestSendModeFilter:
    def test_silent_source_excluded(self):
        """A source with send_mode=silent should never be selected."""
        from agents.engagement import selector

        c = _candidate(source="wiki_new_file")
        ctx = _make_ctx(enabled={"wiki_new_file"})

        with patch("agents.engagement.selector._source_send_mode", return_value="silent"):
            result = selector.select([c], ctx)
        assert result is None

    def test_observation_source_passes(self):
        """A source with send_mode=observation should be selected (not filtered)."""
        from agents.engagement import selector

        c = _candidate(source="wiki_new_file")
        ctx = _make_ctx(enabled={"wiki_new_file"})

        with (
            patch("agents.engagement.selector._source_send_mode", return_value="observation"),
            patch("agents.engagement.selector._source_min_value_score", return_value=0.0),
            patch("agents.engagement.selector._value_score", return_value=0.5),
            patch("agents.engagement.selector._hard_interval_blocked", return_value=False),
            patch("agents.engagement.selector._get_cofire_state", return_value=(None, None)),
            patch("agents.engagement.selector._set_cofire_state"),
        ):
            result = selector.select([c], ctx)
        assert result is not None
        assert result.source == "wiki_new_file"

    def test_proactive_source_passes(self):
        """A source with send_mode=proactive should not be filtered by mode."""
        from agents.engagement import selector

        c = _candidate(source="gmail_unread_threshold", pool="user_anchored")
        ctx = _make_ctx(enabled={"gmail_unread_threshold"})

        with (
            patch("agents.engagement.selector._source_send_mode", return_value="proactive"),
            patch("agents.engagement.selector._source_min_value_score", return_value=0.0),
            patch("agents.engagement.selector._value_score", return_value=0.6),
            patch("agents.engagement.selector._hard_interval_blocked", return_value=False),
            patch("agents.engagement.selector._get_cofire_state", return_value=(None, None)),
            patch("agents.engagement.selector._set_cofire_state"),
        ):
            result = selector.select([c], ctx)
        assert result is not None


# ---------------------------------------------------------------------------
# value_score filter
# ---------------------------------------------------------------------------

class TestValueScoreFilter:
    def test_below_min_value_score_filtered(self):
        """Candidate with value_score < min_value_score should be filtered out."""
        from agents.engagement import selector

        c = _candidate(source="wiki_new_file")
        ctx = _make_ctx(enabled={"wiki_new_file"})

        with (
            patch("agents.engagement.selector._source_send_mode", return_value="observation"),
            patch("agents.engagement.selector._source_min_value_score", return_value=0.9),
            patch("agents.engagement.selector._value_score", return_value=0.2),
            patch("agents.engagement.selector._hard_interval_blocked", return_value=False),
        ):
            result = selector.select([c], ctx)
        assert result is None

    def test_above_min_value_score_passes(self):
        """Candidate with value_score >= min_value_score should pass."""
        from agents.engagement import selector

        c = _candidate(source="wiki_new_file")
        ctx = _make_ctx(enabled={"wiki_new_file"})

        with (
            patch("agents.engagement.selector._source_send_mode", return_value="observation"),
            patch("agents.engagement.selector._source_min_value_score", return_value=0.3),
            patch("agents.engagement.selector._value_score", return_value=0.5),
            patch("agents.engagement.selector._hard_interval_blocked", return_value=False),
            patch("agents.engagement.selector._get_cofire_state", return_value=(None, None)),
            patch("agents.engagement.selector._set_cofire_state"),
        ):
            result = selector.select([c], ctx)
        assert result is not None


# ---------------------------------------------------------------------------
# _value_score rubric
# ---------------------------------------------------------------------------

class TestValueScoreRubric:
    def test_value_score_in_range(self):
        """value_score must be in [0, 1]."""
        from zoneinfo import ZoneInfo

        from agents.engagement import selector

        c = _candidate(source="wiki_new_file", payload={"filename": "foo.md"})
        ctx = SimpleNamespace(
            now_local=datetime.now(ZoneInfo("UTC")).replace(hour=10),
            mood="focused",
            enabled_sources={"wiki_new_file"},
            pool_caps={"user_anchored": True},
            source_response_rate={},
            last_send_per_source={},
        )

        with patch("agents.proactive._is_quiet_now", return_value=False):
            vs = selector._value_score(c, ctx)
        assert 0.0 <= vs <= 1.0

    def test_value_score_higher_with_anchor(self):
        """Candidate with anchor payload key should score higher than one without."""
        from zoneinfo import ZoneInfo

        from agents.engagement import selector

        ctx = SimpleNamespace(
            now_local=datetime.now(ZoneInfo("UTC")).replace(hour=10),
            mood="focused",
            enabled_sources=set(),
            pool_caps={},
            source_response_rate={},
            last_send_per_source={},
        )

        c_with_anchor = TriggerCandidate(
            source="wiki_new_file", pool="user_anchored", pattern="notify",
            payload={"filename": "foo.md"}, dedup_key="test:with",
            decay_at=datetime.now(UTC) + timedelta(hours=1),
            novelty=0.7, actionability=0.6, confidence=0.8,
        )
        c_no_anchor = TriggerCandidate(
            source="wiki_new_file", pool="user_anchored", pattern="notify",
            payload={"folder": "docs"},  # no "filename" key
            dedup_key="test:without",
            decay_at=datetime.now(UTC) + timedelta(hours=1),
            novelty=0.7, actionability=0.6, confidence=0.8,
        )

        with patch("agents.proactive._is_quiet_now", return_value=False):
            vs_with = selector._value_score(c_with_anchor, ctx)
            vs_without = selector._value_score(c_no_anchor, ctx)
        assert vs_with > vs_without

    def test_value_score_lower_during_quiet_hours(self):
        """value_score timing component drops when quiet hours are active."""
        from zoneinfo import ZoneInfo

        from agents.engagement import selector

        ctx = SimpleNamespace(
            now_local=datetime.now(ZoneInfo("UTC")),
            mood="focused",
            enabled_sources=set(),
            pool_caps={},
            source_response_rate={},
            last_send_per_source={},
        )
        c = _candidate(source="wiki_new_file", payload={"filename": "foo.md"})

        with patch("agents.proactive._is_quiet_now", return_value=False):
            vs_awake = selector._value_score(c, ctx)

        with patch("agents.proactive._is_quiet_now", return_value=True):
            vs_quiet = selector._value_score(c, ctx)

        assert vs_quiet < vs_awake


# ---------------------------------------------------------------------------
# Co-firing guard
# ---------------------------------------------------------------------------

class TestCofireGuard:
    def test_select_does_not_mutate_cofire_state(self):
        """select() must not call _set_cofire_state — cofire state is committed
        post-send via commit_cofire(), not during selection."""
        from agents.engagement import selector

        c1 = _candidate(source="wiki_new_file")
        c2 = _candidate(source="gmail_unread_threshold", pool="user_anchored")
        ctx = _make_ctx(enabled={"wiki_new_file", "gmail_unread_threshold"})

        last_iso = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
        set_calls = []

        with (
            patch("agents.engagement.selector._source_send_mode", return_value="proactive"),
            patch("agents.engagement.selector._source_min_value_score", return_value=0.0),
            patch("agents.engagement.selector._value_score", return_value=0.5),
            patch("agents.engagement.selector._hard_interval_blocked", return_value=False),
            patch("agents.engagement.selector._get_cofire_state", return_value=(last_iso, "some_source")),
            patch("agents.engagement.selector._set_cofire_state", side_effect=set_calls.append),
        ):
            result = selector.select([c1, c2], ctx)

        assert result is not None
        assert len(set_calls) == 0, "select() must not write cofire state"

    def test_cofire_detected_within_60s_logs_but_does_not_hold(self):
        """Within 60s, co-fire is detected (logged); second candidate is silently
        dropped — no hold write occurs (hold was removed as write-only dead code)."""
        from agents.engagement import selector

        c1 = _candidate(source="wiki_new_file")
        c2 = _candidate(source="gmail_unread_threshold", pool="user_anchored")
        ctx = _make_ctx(enabled={"wiki_new_file", "gmail_unread_threshold"})

        last_iso = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()

        with (
            patch("agents.engagement.selector._source_send_mode", return_value="proactive"),
            patch("agents.engagement.selector._source_min_value_score", return_value=0.0),
            patch("agents.engagement.selector._value_score", return_value=0.5),
            patch("agents.engagement.selector._hard_interval_blocked", return_value=False),
            patch("agents.engagement.selector._get_cofire_state", return_value=(last_iso, "some_source")),
            patch("agents.engagement.selector._set_cofire_state"),
        ):
            result = selector.select([c1, c2], ctx)

        # Best candidate is still returned; no exception raised
        assert result is not None
        assert result.source in {"wiki_new_file", "gmail_unread_threshold"}

    def test_no_cofire_when_gap_exceeds_60s(self):
        """When the last send was >60s ago, no co-fire is detected."""
        from agents.engagement import selector

        c1 = _candidate(source="wiki_new_file")
        c2 = _candidate(source="gmail_unread_threshold", pool="user_anchored")
        ctx = _make_ctx(enabled={"wiki_new_file", "gmail_unread_threshold"})

        last_iso = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()

        with (
            patch("agents.engagement.selector._source_send_mode", return_value="proactive"),
            patch("agents.engagement.selector._source_min_value_score", return_value=0.0),
            patch("agents.engagement.selector._value_score", return_value=0.5),
            patch("agents.engagement.selector._hard_interval_blocked", return_value=False),
            patch("agents.engagement.selector._get_cofire_state", return_value=(last_iso, "some_source")),
            patch("agents.engagement.selector._set_cofire_state"),
        ):
            result = selector.select([c1, c2], ctx)

        assert result is not None

    def test_commit_cofire_writes_state(self):
        """commit_cofire(source) writes cofire state to runtime_state."""
        from agents.engagement import selector
        from storage import db

        selector.commit_cofire("wiki_new_file")

        iso = db.runtime_get(selector._COFIRE_KEY)
        src = db.runtime_get(selector._COFIRE_SOURCE_KEY)
        assert iso is not None
        assert src == "wiki_new_file"

    def test_commit_cofire_is_public(self):
        """commit_cofire must be a public attribute of the selector module."""
        import agents.engagement.selector as sel
        assert hasattr(sel, "commit_cofire")
        assert callable(sel.commit_cofire)


# ---------------------------------------------------------------------------
# on_reaction
# ---------------------------------------------------------------------------

class TestOnReaction:
    def test_thumbs_up_increments_counter_and_adjusts_ema(self):
        """on_reaction('up') increments thumbs_up and shifts EMA toward 1.0."""
        from agents.engagement.sender import on_reaction
        from storage import db

        on_reaction("wiki_new_file", "up")

        rows = db.proactive_source_scores_all()
        assert any(r["source"] == "wiki_new_file" for r in rows), "row not created"
        row = next(r for r in rows if r["source"] == "wiki_new_file")
        assert row["n_thumbs_up"] >= 1
        # EMA should have moved toward 1.0 from the default 0.5
        assert row["ema"] > 0.5

    def test_thumbs_down_increments_counter_and_adjusts_ema(self):
        """on_reaction('down') increments thumbs_down and shifts EMA toward 0.0."""
        from agents.engagement.sender import on_reaction
        from storage import db

        on_reaction("gmail_unread_threshold", "down")

        rows = db.proactive_source_scores_all()
        row = next((r for r in rows if r["source"] == "gmail_unread_threshold"), None)
        assert row is not None
        assert row["n_thumbs_down"] >= 1
        assert row["ema"] < 0.5

    def test_reaction_is_idempotent_across_multiple_calls(self):
        """Multiple up reactions keep incrementing counters."""
        from agents.engagement.sender import on_reaction
        from storage import db

        on_reaction("calendar_event_prep", "up")
        on_reaction("calendar_event_prep", "up")
        on_reaction("calendar_event_prep", "up")

        rows = db.proactive_source_scores_all()
        row = next(r for r in rows if r["source"] == "calendar_event_prep")
        assert row["n_thumbs_up"] == 3


# ---------------------------------------------------------------------------
# defer_to_next_turn outcome
# ---------------------------------------------------------------------------

class TestDeferToNextTurn:
    def test_defer_writes_to_deferred_observations(self):
        """[[defer:next_turn]] in text writes to deferred_observations runtime key."""
        from agents.engagement import sender
        from storage import db

        class _FakeCandidate:
            source = "wiki_new_file"
            pattern = "notify"
            payload = {"filename": "foo.md"}

        # Patch session_scratch insert to avoid schema dependency
        with patch("agents.engagement.sender._write_defer_scratch", wraps=sender._write_defer_scratch):
            # Call _write_defer_scratch directly with kind="next_turn"
            sender._write_defer_scratch("next_turn", "foo.md just dropped.", _FakeCandidate())

        raw = db.runtime_get("deferred_observations")
        assert raw is not None
        obs = json.loads(raw)
        assert isinstance(obs, list)
        assert len(obs) >= 1
        assert obs[-1]["text"] == "foo.md just dropped."
        assert obs[-1]["source"] == "wiki_new_file"

    def test_defer_appends_not_overwrites(self):
        """Multiple defer_to_next_turn calls append to the list."""
        from agents.engagement import sender
        from storage import db

        class _FakeCandidate:
            source = "wiki_new_file"
            pattern = "notify"
            payload = {}

        sender._write_defer_scratch("next_turn", "first observation", _FakeCandidate())
        sender._write_defer_scratch("next_turn", "second observation", _FakeCandidate())

        raw = db.runtime_get("deferred_observations")
        obs = json.loads(raw)
        texts = [o["text"] for o in obs]
        assert "first observation" in texts
        assert "second observation" in texts


class TestReengageSilenceValueGate:
    """Regression for the reengage_silence value-score gate (2026-06-03 fix).

    reengage_silence has no anchor token (ANCHOR_TOKEN_PATHS[...] == ()), so its
    value_score is 0.3075 + 0.15*timing → 0.4575 (peak) / 0.3825 (off) / 0.3225
    (quiet). The old min_value_score of 0.5 was unreachable; 0.35 lets it fire
    peak+off while still blocking quiet hours. Exercises the REAL gate — no
    patching of _value_score / _source_min_value_score (which masked the bug).
    """

    def _reengage_candidate(self) -> TriggerCandidate:
        # Mirror agents/engagement/producers/reengage_silence.py field values.
        return TriggerCandidate(
            source="reengage_silence",
            pool="agent_spontaneous",
            pattern="notify",
            payload={"elapsed_hours": 5.0, "last_message_ts": "2026-06-03T10:00:00+00:00"},
            dedup_key="reengage_silence:test",
            decay_at=datetime.now(UTC) + timedelta(hours=1),
            novelty=0.5,
            actionability=0.6,
            confidence=0.8,
        )

    def test_fires_at_peak_hours(self):
        from agents.engagement import selector

        ctx = _make_ctx(
            enabled={"reengage_silence"},
            pool_caps={"agent_spontaneous": True},
        )
        ctx.now_local = datetime(2026, 6, 3, 20, 0, tzinfo=UTC)  # 20:00 = preferred
        with patch("agents.proactive._is_quiet_now", return_value=False):
            result = selector.select([self._reengage_candidate()], ctx)
        assert result is not None
        assert result.source == "reengage_silence"

    def test_blocked_during_quiet_hours(self):
        from agents.engagement import selector

        ctx = _make_ctx(
            enabled={"reengage_silence"},
            pool_caps={"agent_spontaneous": True},
        )
        ctx.now_local = datetime(2026, 6, 3, 3, 0, tzinfo=UTC)  # quiet window
        with patch("agents.proactive._is_quiet_now", return_value=True):
            result = selector.select([self._reengage_candidate()], ctx)
        assert result is None
