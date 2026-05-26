"""Tests for send_mode / priority-tier ordering in selector.select().

Sprint B Wave 3 — tests-engagement-policy agent.

The sprint plan calls the tier-1 sources "alarm" and tier-2 sources "ambient".
In code these map to priority_tier=1 (multiplier 1.5×) vs priority_tier=2
(multiplier 1.0×) configured per source in config/engagement.yaml.

Coverage:
  1. Calendar prep (tier-1 / "alarm") beats mood-leak (tier-2 / "ambient")
     when both are in the same tick.
  2. reminder_fire (tier-1) beats gmail_unread_threshold (tier-1 but lower
     scoring in isolation) through exact-reminder priority.
  3. Two candidates at the same tick → the one with the higher priority_tier
     multiplier wins selection.
"""
from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agents.engagement.triggers import TriggerCandidate


# ---------------------------------------------------------------------------
# Shared fixtures (pattern identical to test_engagement_selector.py)
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
    source: str,
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
        payload=payload or {"title": "standup"},
        dedup_key=f"{source}:test",
        decay_at=datetime.now(UTC) + timedelta(hours=1),
        novelty=novelty,
        actionability=actionability,
        confidence=confidence,
    )


def _ctx(
    enabled: set[str] | None = None,
    mood: str = "focused",
    pool_caps: dict[str, bool] | None = None,
    last_send: dict[str, str] | None = None,
) -> SimpleNamespace:
    from zoneinfo import ZoneInfo
    return SimpleNamespace(
        now_local=datetime.now(ZoneInfo("UTC")),
        mood=mood,
        enabled_sources=enabled or {
            "calendar_event_prep",
            "weirdly_good_mood_leak",
            "reminder_fire",
            "gmail_unread_threshold",
        },
        pool_caps=pool_caps or {"user_anchored": True, "agent_spontaneous": True},
        source_response_rate={},
        last_send_per_source=last_send or {},
    )


# ---------------------------------------------------------------------------
# 1. Alarm (tier-1) beats ambient (tier-2)
# ---------------------------------------------------------------------------

class TestAlarmBeatsAmbient:
    """calendar_event_prep (tier-1) must win over weirdly_good_mood_leak (tier-2)."""

    def test_calendar_prep_beats_mood_leak(self):
        """With identical base scores, tier-1 multiplier (1.5×) beats tier-2 (1.0×)."""
        from agents.engagement import selector

        cal = _candidate("calendar_event_prep", payload={"title": "standup", "minutes_until": 15})
        mood_leak = _candidate("weirdly_good_mood_leak", pool="agent_spontaneous", payload={})

        ctx = _ctx(enabled={"calendar_event_prep", "weirdly_good_mood_leak"})

        def _fake_send_mode(source: str) -> str:
            return "proactive"

        def _fake_tier_mult(source: str) -> float:
            # Replicate real config: calendar=1.5×, mood_leak=1.0×
            return 1.5 if source == "calendar_event_prep" else 1.0

        with (
            patch("agents.engagement.selector._source_send_mode", side_effect=_fake_send_mode),
            patch("agents.engagement.selector._source_min_value_score", return_value=0.0),
            patch("agents.engagement.selector._value_score", return_value=0.5),
            patch("agents.engagement.selector._hard_interval_blocked", return_value=False),
            patch("agents.engagement.selector._priority_tier_multiplier", side_effect=_fake_tier_mult),
            patch("agents.engagement.selector._get_cofire_state", return_value=(None, None)),
            patch("agents.engagement.selector._set_cofire_state"),
        ):
            result = selector.select([cal, mood_leak], ctx)

        assert result is not None
        assert result.source == "calendar_event_prep", (
            f"Expected calendar_event_prep (tier-1/alarm) to win, got {result.source}"
        )

    def test_mood_leak_wins_when_calendar_absent(self):
        """When only the tier-2 source is available, it still gets selected."""
        from agents.engagement import selector

        mood_leak = _candidate("weirdly_good_mood_leak", pool="agent_spontaneous", payload={})
        ctx = _ctx(enabled={"weirdly_good_mood_leak"})

        with (
            patch("agents.engagement.selector._source_send_mode", return_value="proactive"),
            patch("agents.engagement.selector._source_min_value_score", return_value=0.0),
            patch("agents.engagement.selector._value_score", return_value=0.5),
            patch("agents.engagement.selector._hard_interval_blocked", return_value=False),
            patch("agents.engagement.selector._priority_tier_multiplier", return_value=1.0),
            patch("agents.engagement.selector._get_cofire_state", return_value=(None, None)),
            patch("agents.engagement.selector._set_cofire_state"),
        ):
            result = selector.select([mood_leak], ctx)

        assert result is not None
        assert result.source == "weirdly_good_mood_leak"


# ---------------------------------------------------------------------------
# 2. Reminder (tier-1) beats gmail threshold (tier-1 lower base score)
# ---------------------------------------------------------------------------

class TestReminderBeatsGmail:
    """reminder_fire with high confidence beats gmail_unread_threshold."""

    def test_high_confidence_reminder_beats_gmail(self):
        """reminder_fire at high confidence/actionability must outscore gmail."""
        from agents.engagement import selector

        reminder = _candidate(
            "reminder_fire",
            novelty=0.9,
            actionability=0.9,
            confidence=0.95,
            payload={"text": "call dentist"},
        )
        gmail = _candidate(
            "gmail_unread_threshold",
            novelty=0.5,
            actionability=0.4,
            confidence=0.7,
            payload={"unread_count": 12},
        )

        ctx = _ctx(enabled={"reminder_fire", "gmail_unread_threshold"})

        def _tier(source: str) -> float:
            # Both tier-1; reminder wins on base score
            return 1.5

        with (
            patch("agents.engagement.selector._source_send_mode", return_value="proactive"),
            patch("agents.engagement.selector._source_min_value_score", return_value=0.0),
            patch("agents.engagement.selector._value_score", return_value=0.5),
            patch("agents.engagement.selector._hard_interval_blocked", return_value=False),
            patch("agents.engagement.selector._priority_tier_multiplier", side_effect=_tier),
            patch("agents.engagement.selector._get_cofire_state", return_value=(None, None)),
            patch("agents.engagement.selector._set_cofire_state"),
        ):
            result = selector.select([reminder, gmail], ctx)

        assert result is not None
        assert result.source == "reminder_fire", (
            f"reminder_fire should outscore gmail via higher novelty/actionability/confidence, got {result.source}"
        )


# ---------------------------------------------------------------------------
# 3. Higher send_mode / tier wins when two candidates fire at same tick
# ---------------------------------------------------------------------------

class TestHigherTierWinsAtSameTick:
    """Two candidates in same tick: the one with the higher priority_tier_multiplier wins."""

    def test_tier1_beats_tier3(self):
        """Tier-1 (1.5×) vs tier-3 (0.6×) — tier-1 wins even with equal base fields."""
        from agents.engagement import selector

        tier1 = _candidate(
            "calendar_event_prep",
            novelty=0.6,
            actionability=0.6,
            confidence=0.8,
            payload={"title": "standup", "minutes_until": 10},
        )
        tier3 = _candidate(
            "reengage_silence",
            pool="agent_spontaneous",
            novelty=0.6,
            actionability=0.6,
            confidence=0.8,
            payload={},
        )

        ctx = _ctx(enabled={"calendar_event_prep", "reengage_silence"})

        def _tier(source: str) -> float:
            return 1.5 if source == "calendar_event_prep" else 0.6

        with (
            patch("agents.engagement.selector._source_send_mode", return_value="proactive"),
            patch("agents.engagement.selector._source_min_value_score", return_value=0.0),
            patch("agents.engagement.selector._value_score", return_value=0.5),
            patch("agents.engagement.selector._hard_interval_blocked", return_value=False),
            patch("agents.engagement.selector._priority_tier_multiplier", side_effect=_tier),
            patch("agents.engagement.selector._get_cofire_state", return_value=(None, None)),
            patch("agents.engagement.selector._set_cofire_state"),
        ):
            result = selector.select([tier1, tier3], ctx)

        assert result is not None
        assert result.source == "calendar_event_prep", (
            f"Expected tier-1 winner, got {result.source}"
        )

    def test_both_tier1_score_breaks_tie(self):
        """Two tier-1 sources: higher base score (novelty/actionability) wins."""
        from agents.engagement import selector

        strong = _candidate(
            "calendar_event_prep",
            novelty=0.9,
            actionability=0.9,
            confidence=0.9,
            payload={"title": "board meeting", "minutes_until": 5},
        )
        weak = _candidate(
            "reminder_fire",
            novelty=0.3,
            actionability=0.3,
            confidence=0.5,
            payload={"text": "water plants"},
        )

        ctx = _ctx(enabled={"calendar_event_prep", "reminder_fire"})

        with (
            patch("agents.engagement.selector._source_send_mode", return_value="proactive"),
            patch("agents.engagement.selector._source_min_value_score", return_value=0.0),
            patch("agents.engagement.selector._value_score", return_value=0.5),
            patch("agents.engagement.selector._hard_interval_blocked", return_value=False),
            patch("agents.engagement.selector._priority_tier_multiplier", return_value=1.5),
            patch("agents.engagement.selector._get_cofire_state", return_value=(None, None)),
            patch("agents.engagement.selector._set_cofire_state"),
        ):
            result = selector.select([strong, weak], ctx)

        assert result is not None
        assert result.source == "calendar_event_prep", (
            f"Expected higher-scoring tier-1 to win, got {result.source}"
        )

    def test_silent_source_excluded_regardless_of_tier(self):
        """A silent send_mode is excluded even if it would have won on score."""
        from agents.engagement import selector

        silent_tier1 = _candidate(
            "calendar_event_prep",
            novelty=1.0,
            actionability=1.0,
            confidence=1.0,
            payload={"title": "secret", "minutes_until": 1},
        )
        proactive_tier2 = _candidate(
            "weirdly_good_mood_leak",
            pool="agent_spontaneous",
            novelty=0.5,
            actionability=0.5,
            confidence=0.5,
            payload={},
        )

        ctx = _ctx(enabled={"calendar_event_prep", "weirdly_good_mood_leak"})

        def _mode(source: str) -> str:
            return "silent" if source == "calendar_event_prep" else "proactive"

        with (
            patch("agents.engagement.selector._source_send_mode", side_effect=_mode),
            patch("agents.engagement.selector._source_min_value_score", return_value=0.0),
            patch("agents.engagement.selector._value_score", return_value=0.5),
            patch("agents.engagement.selector._hard_interval_blocked", return_value=False),
            patch("agents.engagement.selector._priority_tier_multiplier", return_value=1.5),
            patch("agents.engagement.selector._get_cofire_state", return_value=(None, None)),
            patch("agents.engagement.selector._set_cofire_state"),
        ):
            result = selector.select([silent_tier1, proactive_tier2], ctx)

        assert result is not None
        assert result.source == "weirdly_good_mood_leak", (
            f"Silent source should be excluded; expected mood_leak to win, got {result.source}"
        )
