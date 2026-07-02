"""Phase I proactive engagement tests.

Coverage:
  - One test per producer (13 tests): mock data layer, assert collect() shape.
  - Selector: highest-score wins, disabled sources excluded, pool cap respected.
  - Guard: generic opener, missing anchor, well-formed, question-pattern.
  - proactive status formatter: active-source listing, default count.
  - Config: default_enabled_sources has exactly 11 entries (Sprint 1, 2026-07-02:
    weirdly_good_mood_leak / irritation_event demoted — no payload, contentless).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from agents.engagement.triggers import TriggerCandidate

# ---------- helpers ----------

def _make_candidate(
    source: str = "wiki_new_file",
    pool: str = "user_anchored",
    pattern: str = "notify",
    payload: dict | None = None,
    novelty: float = 0.5,
    actionability: float = 0.5,
    confidence: float = 0.8,
) -> TriggerCandidate:
    return TriggerCandidate(
        source=source,
        pool=pool,
        pattern=pattern,
        payload=payload or {},
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
    response_rate: dict[str, float] | None = None,
    last_send: dict[str, str] | None = None,
) -> SimpleNamespace:
    from zoneinfo import ZoneInfo
    return SimpleNamespace(
        now_local=datetime.now(ZoneInfo("UTC")),
        mood=mood,
        enabled_sources=enabled or {"wiki_new_file"},
        pool_caps=pool_caps or {"user_anchored": True, "agent_spontaneous": True},
        source_response_rate=response_rate or {},
        last_send_per_source=last_send or {},
    )


# ============================================================================
# Producer tests (1 per producer)
# ============================================================================

class TestProducerWikiNewFile:
    def test_collect_returns_empty_when_no_wiki(self):
        from agents.engagement.producers import wiki_new_file
        with patch.object(wiki_new_file, "_wiki_root", return_value=None):
            assert wiki_new_file.collect() == []

    def test_collect_returns_candidates_for_new_files(self, tmp_path):
        from agents.engagement.producers import wiki_new_file
        md = tmp_path / "test-note.md"
        md.write_text("# Hello\n", encoding="utf-8")
        with (
            patch.object(wiki_new_file, "_wiki_root", return_value=tmp_path),
            patch("storage.db.runtime_get", return_value=None),
        ):
            results = wiki_new_file.collect()
        assert isinstance(results, list)
        assert all(c.source == "wiki_new_file" for c in results)


# NOTE: TestProducerGmailUnreadThreshold removed 2026-06-01 — the
# gmail_unread_threshold producer was deleted (it read a runtime_state key no
# code ever wrote, so it was permanently dead). See tools/gmail/inbox.py for
# the typed read path that replaced the LLM-delegated inbox fetch.


class TestProducerCalendarEventPrep:
    def test_collect_empty_when_mcp_cold(self):
        from agents.engagement.producers import calendar_event_prep
        with patch("agents.mcp_manager.MANAGER.is_warm", return_value=False):
            assert calendar_event_prep.collect() == []

    def test_collect_empty_when_no_data(self):
        from agents.engagement.producers import calendar_event_prep
        with (
            patch("agents.mcp_manager.MANAGER.is_warm", return_value=True),
            patch("storage.db.runtime_get", return_value=None),
        ):
            assert calendar_event_prep.collect() == []

    def test_collect_returns_candidate_in_lead_window(self):
        from agents.engagement.producers import calendar_event_prep
        # Event starts in 30 minutes (within the ±5 jitter window)
        start = datetime.now(UTC) + timedelta(minutes=30)
        events = json.dumps([{"id": "evt1", "title": "Stand-up", "start_iso": start.isoformat()}])
        with (
            patch("agents.mcp_manager.MANAGER.is_warm", return_value=True),
            patch("storage.db.runtime_get", return_value=events),
            patch("storage.db.calendar_notification_exists", return_value=False),
        ):
            results = calendar_event_prep.collect()
        assert len(results) == 1
        assert results[0].source == "calendar_event_prep"
        assert "Stand-up" in results[0].payload["title"]


class TestProducerCalendarNewInvite:
    def test_collect_empty_when_mcp_cold(self):
        from agents.engagement.producers import calendar_new_invite
        with patch("agents.mcp_manager.MANAGER.is_warm", return_value=False):
            assert calendar_new_invite.collect() == []

    def test_collect_returns_candidate_for_new_invite(self):
        from agents.engagement.producers import calendar_new_invite
        invites = json.dumps([{"id": "inv1", "title": "Team sync", "organizer": "alice@example.com"}])
        with (
            patch("agents.mcp_manager.MANAGER.is_warm", return_value=True),
            patch("agents.config.get", return_value=True),
            patch("storage.db.runtime_get", side_effect=lambda k: invites if k == "calendar_pending_invites" else None),
        ):
            results = calendar_new_invite.collect()
        assert len(results) == 1
        assert results[0].source == "calendar_new_invite"
        assert results[0].payload["title"] == "Team sync"


class TestProducerReminderFire:
    def test_collect_empty_when_none_due(self):
        from agents.engagement.producers import reminder_fire
        with patch("storage.db.reminder_due", return_value=[]):
            assert reminder_fire.collect() == []

    def test_collect_returns_empty_when_disabled(self):
        # The producer must still honor enabled: false (it ships enabled +
        # send_mode: silent since the silent-awareness change; the flag is the
        # operator kill switch).
        from agents import config as _cfg
        from agents.engagement.producers import reminder_fire
        due = [{"id": 1, "text": "call dentist", "fire_at": datetime.now(UTC).isoformat()}]
        with (
            patch("storage.db.reminder_due", return_value=due),
            patch.object(_cfg, "get", side_effect=lambda k, d=None: (
                False if k == "engagement.reminder_fire.enabled" else d)),
        ):
            results = reminder_fire.collect()
        assert results == [], (
            "reminder_fire producer must return empty when disabled in config"
        )

    def test_collect_returns_candidate_when_enabled_override(self):
        # Verify the producer still works when explicitly enabled (e.g. for testing).
        from agents import config as _cfg
        from agents.engagement.producers import reminder_fire
        due = [{"id": 1, "text": "call dentist", "fire_at": datetime.now(UTC).isoformat()}]
        with (
            patch("storage.db.reminder_due", return_value=due),
            patch.object(_cfg, "get", side_effect=lambda k, d=None: True if k == "engagement.reminder_fire.enabled" else d),
        ):
            results = reminder_fire.collect()
        assert len(results) == 1
        assert results[0].source == "reminder_fire"
        assert results[0].payload["text"] == "call dentist"


class TestProducerDecisionResolveDue:
    def test_collect_empty_when_none_due(self):
        from agents.engagement.producers import decision_resolve_due
        with patch("storage.db.decisions_unresolved_due", return_value=[]):
            assert decision_resolve_due.collect() == []

    def test_collect_returns_candidate_for_due_decision(self):
        from agents.engagement.producers import decision_resolve_due
        rows = [{"id": 5, "statement": "ship by friday at 70%",
                 "predicted_p": 0.7, "resolve_by": "2026-05-23"}]
        with patch("storage.db.decisions_unresolved_due", return_value=rows):
            results = decision_resolve_due.collect()
        assert len(results) == 1
        assert results[0].source == "decision_resolve_due"
        assert results[0].payload["statement"] == "ship by friday at 70%"


class TestProducerCallbackEpisode:
    def test_collect_empty_when_no_candidate(self):
        from agents.engagement.producers import callback_episode
        with (
            patch("agents.callback_surface.pick_callback_candidate", return_value=None),
            patch("storage.db.runtime_get", return_value=None),
        ):
            assert callback_episode.collect() == []

    def test_collect_returns_candidate(self):
        from agents.engagement.producers import callback_episode
        ep = {"id": "ep:42", "text": "talked about moving to oslo", "date": "2026-05-01", "score": 0.4}
        with (
            patch("agents.callback_surface.pick_callback_candidate", return_value=ep),
            patch("agents.config.get", return_value=True),
            patch("storage.db.runtime_get", return_value=None),
        ):
            results = callback_episode.collect()
        assert len(results) == 1
        assert results[0].source == "callback_episode"


class TestProducerDriveStarredNew:
    def test_collect_empty_when_mcp_cold(self):
        from agents.engagement.producers import drive_starred_new
        with patch("agents.mcp_manager.MANAGER.is_warm", return_value=False):
            assert drive_starred_new.collect() == []

    def test_collect_returns_candidate_for_new_starred_file(self):
        from agents.engagement.producers import drive_starred_new
        files = json.dumps([{"id": "file1", "name": "Q2 report.pdf"}])
        with (
            patch("agents.mcp_manager.MANAGER.is_warm", return_value=True),
            patch("agents.config.get", return_value=True),
            patch("storage.db.runtime_get", side_effect=lambda k: files if k == "drive_starred_files" else None),
        ):
            results = drive_starred_new.collect()
        assert len(results) == 1
        assert results[0].source == "drive_starred_new"
        assert results[0].payload["name"] == "Q2 report.pdf"


class TestProducerNotionRecentEdit:
    def test_collect_empty_when_mcp_cold(self):
        from agents.engagement.producers import notion_recent_edit
        with patch("agents.mcp_manager.MANAGER.is_warm", return_value=False):
            assert notion_recent_edit.collect() == []

    def test_collect_returns_candidate_for_edited_page(self):
        from agents.engagement.producers import notion_recent_edit
        pages = json.dumps([{"id": "pg1", "title": "Sprint notes"}])
        with (
            patch("agents.mcp_manager.MANAGER.is_warm", return_value=True),
            patch("agents.config.get", return_value=True),
            patch("storage.db.runtime_get", side_effect=lambda k: pages if k == "notion_recent_edits" else None),
        ):
            results = notion_recent_edit.collect()
        assert len(results) == 1
        assert results[0].source == "notion_recent_edit"
        assert results[0].payload["page_title"] == "Sprint notes"


class TestProducerWeatherAlert:
    def test_collect_empty_when_no_alert(self):
        from agents.engagement.producers import weather_alert
        with patch("storage.db.runtime_get", return_value=None):
            assert weather_alert.collect() == []

    def test_collect_returns_candidate_for_alert(self):
        from agents.engagement.producers import weather_alert
        alert_data = json.dumps({"alert_summary": "wind gusts 25m/s expected"})
        with (
            patch("agents.config.get", return_value=True),
            patch("storage.db.runtime_get", side_effect=lambda k: alert_data if k == "weather_alert_pending" else None),
        ):
            results = weather_alert.collect()
        assert len(results) == 1
        assert results[0].source == "weather_alert"
        assert "wind gusts" in results[0].payload["alert_summary"]


class TestProducerWeirdlyGoodMoodLeak:
    def test_collect_empty_when_mood_not_weirdly_good(self):
        from agents.engagement.producers import weirdly_good_mood_leak
        with patch("storage.db.get_core_block", return_value="focused"):
            assert weirdly_good_mood_leak.collect() == []

    def test_collect_returns_candidate_when_mood_weirdly_good(self):
        from agents.engagement.producers import weirdly_good_mood_leak
        with (
            patch("agents.config.get", return_value=True),
            patch("storage.db.get_core_block", return_value="weirdly good"),
            patch("storage.db.runtime_get", return_value=None),
        ):
            results = weirdly_good_mood_leak.collect()
        assert len(results) == 1
        assert results[0].source == "weirdly_good_mood_leak"


class TestProducerLocationArrivedRecurring:
    def test_collect_empty_when_no_pattern(self):
        from agents.engagement.producers import location_arrived_recurring
        with (
            patch("agents.proactive.detect_recurring_location_pattern", return_value=None),
            patch("storage.db.runtime_get", return_value=None),
        ):
            assert location_arrived_recurring.collect() == []

    def test_collect_returns_candidate_when_pattern_detected(self):
        from agents.engagement.producers import location_arrived_recurring
        pattern = {"lat": 59.913, "lon": 10.752, "label": "Office", "visit_count": 5}
        with (
            patch("agents.config.get", return_value=True),
            patch("agents.proactive.detect_recurring_location_pattern", return_value=pattern),
            patch("storage.db.runtime_get", return_value=None),
        ):
            results = location_arrived_recurring.collect()
        assert len(results) == 1
        assert results[0].source == "location_arrived_recurring"
        assert results[0].payload["place_name"] == "Office"


# NOTE: TestProducerGmailImportantThread removed 2026-06-01 — the
# gmail_important_thread producer was deleted (dead: it read a runtime_state
# key no code ever wrote). Replaced by the typed read path in tools/gmail/inbox.py.


# ============================================================================
# Selector tests
# ============================================================================

class TestSelector:
    def test_picks_highest_score(self):
        from agents.engagement.selector import select
        # Sprint A added per-source min_value_score with an anchor weight in
        # the value rubric — supply payload anchors so each candidate clears
        # its source's min_value_score and only the score ordering is tested.
        low = _make_candidate(
            "reminder_fire", novelty=0.99, actionability=0.99, confidence=0.99,
            payload={"text": "reminder body"},
        )
        mid = _make_candidate(
            "wiki_new_file", novelty=0.5, actionability=0.5, confidence=0.5,
            payload={"filename": "note.md"},
        )
        high = _make_candidate(
            "decision_resolve_due", novelty=0.9, actionability=0.9, confidence=0.9,
            payload={"statement": "ship X by friday"},
        )
        ctx = _make_ctx(enabled={"reminder_fire", "wiki_new_file", "decision_resolve_due"})
        winner = select([low, mid, high], ctx)
        assert winner is not None
        assert winner.source == "decision_resolve_due"

    def test_excludes_disabled_sources(self):
        from agents.engagement.selector import select
        high = _make_candidate("calendar_new_invite", novelty=0.99, actionability=0.99, confidence=0.99)
        # Sprint A added per-source min_value_score (wiki_new_file=0.3); bump
        # the only enabled candidate above threshold so we test the enabled
        # filter, not the new value-score gate.
        low = _make_candidate("wiki_new_file", novelty=0.6, actionability=0.6, confidence=0.6)
        # calendar_new_invite is NOT in enabled set
        ctx = _make_ctx(enabled={"wiki_new_file"})
        winner = select([high, low], ctx)
        assert winner is not None
        assert winner.source == "wiki_new_file"

    def test_excludes_source_not_in_enabled(self):
        from agents.engagement.selector import select
        c = _make_candidate("calendar_new_invite", novelty=0.9, actionability=0.9, confidence=0.9)
        ctx = _make_ctx(enabled=set())  # nothing enabled
        assert select([c], ctx) is None

    def test_respects_pool_cap(self):
        from agents.engagement.selector import select
        c = _make_candidate("wiki_new_file", pool="user_anchored",
                            novelty=0.9, actionability=0.9, confidence=0.9)
        ctx = _make_ctx(
            enabled={"wiki_new_file"},
            pool_caps={"user_anchored": False, "agent_spontaneous": True},
        )
        assert select([c], ctx) is None

    def test_returns_none_when_no_candidates(self):
        from agents.engagement.selector import select
        ctx = _make_ctx()
        assert select([], ctx) is None


# ============================================================================
# Guard tests
# ============================================================================

class TestGuard:
    def test_rejects_generic_opener(self):
        from agents.engagement.guard import passes
        c = _make_candidate("weirdly_good_mood_leak")
        ok, reason = passes("hey, want to chat?", c)
        assert not ok
        assert reason == "generic_opener"

    def test_rejects_hi_opener(self):
        from agents.engagement.guard import passes
        c = _make_candidate("weirdly_good_mood_leak")
        ok, reason = passes("Hi there, just checking in", c)
        assert not ok
        assert reason == "generic_opener"

    def test_rejects_missing_anchor_calendar(self):
        from agents.engagement.guard import passes
        c = _make_candidate("calendar_event_prep", payload={"title": "standup sync"})
        # Text doesn't contain the title verbatim
        ok, reason = passes("you have something later today.", c)
        assert not ok
        assert reason.startswith("missing_anchor")

    def test_accepts_anchor_present_calendar(self):
        from agents.engagement.guard import passes
        c = _make_candidate("calendar_event_prep", payload={"title": "standup sync"})
        ok, reason = passes("standup sync at 14:00. want me to prep?", c)
        assert ok
        assert reason == "ok"

    def test_accepts_well_formed_wiki(self):
        from agents.engagement.guard import passes
        c = _make_candidate("wiki_new_file", pattern="notify",
                            payload={"filename": "oslo-notes.md"})
        ok, reason = passes("new page — 'oslo-notes.md'. so oslo trip notes is a thing now. noted.", c)
        assert ok

    def test_accepts_wiki_composer_template_example_shape(self):
        """Regression: the wiki_new_file composer template's own literal
        example ('example shape: "new page — ... noted."' in composer.py)
        must pass the guard for pattern=notify. This is what broke when the
        producer still emitted pattern=question against a notify-shaped
        template — the guard rejected the template's own example."""
        from agents.engagement.guard import passes
        c = _make_candidate("wiki_new_file", pattern="notify",
                            payload={"filename": "notes.md"})
        ok, reason = passes("new page — 'notes.md'. so kuzu-graph-memory is a thing now. noted.", c)
        assert ok, f"expected ok, got {reason}"

    def test_rejects_question_pattern_no_question_mark(self):
        from agents.engagement.guard import passes
        # Use decision_resolve_due: anchor is "statement" key.
        # If the anchor IS present the guard returns ok — so use a case
        # where the anchor key isn't matched to reach the question-mark check.
        # Easier: use a source with no anchor and pattern=question.
        # Guard only checks pattern for sources WITH anchors after the anchor
        # passes, so let's test a wiki_new_file where anchor IS present but
        # the text doesn't end with the right punctuation.
        # The guard returns True once the anchor matches — so for wiki_new_file
        # we can't test the question check via normal flow.
        # Instead test decision_resolve_due with anchor missing (triggers
        # missing_anchor) — that's already covered. The question-mark guard
        # fires for anchor-free sources with pattern=question.
        # Create a fake source with no anchor paths and pattern=question:
        c = TriggerCandidate(
            source="unknown_future_source",
            pool="user_anchored",
            pattern="question",
            payload={},
            dedup_key="test",
            decay_at=datetime.now(UTC) + timedelta(hours=1),
        )
        ok, reason = passes("you might want to check this.", c)
        assert not ok
        assert reason == "question_pattern_missing_question_mark"

    def test_accepts_no_anchor_required_sources(self):
        from agents.engagement.guard import passes
        for source in ("weirdly_good_mood_leak",):
            c = _make_candidate(source, pattern="notify")
            ok, reason = passes("you went quiet.", c)
            assert ok, f"Expected ok for {source}, got {reason}"

    def test_rejects_empty_text(self):
        from agents.engagement.guard import passes
        c = _make_candidate("wiki_new_file", payload={"filename": "test.md"})
        ok, reason = passes("", c)
        assert not ok
        assert reason == "empty"


# ============================================================================
# proactive status formatter (consumed by the set_proactive_source tool)
# ============================================================================

class TestProactiveStatusFormatter:
    def test_status_lists_sources_with_marks(self):
        """format_proactive_status (now surfaced via set_proactive_source
        action='status') lists every default-enabled source as active —
        including reminder_fire, which is enabled as silent awareness."""
        from agents.cockpit import format_proactive_status
        from agents.engagement.producers import DEFAULT_ENABLED_SOURCES

        with (
            patch("storage.db.runtime_get", return_value=None),
            patch("agents.config.get", return_value=None),
            patch("storage.db.proactive_send_count_7d", return_value=0),
        ):
            text = format_proactive_status()

        assert "active sources" in text
        active_part = text.split("disabled")[0]
        for src in sorted(DEFAULT_ENABLED_SOURCES):
            assert src in active_part, f"{src} should be listed as active"


# ============================================================================
# Config test
# ============================================================================

class TestConfig:
    def test_default_enabled_sources(self):
        from agents import config as cfg
        sources = cfg.get("proactive.default_enabled_sources")
        assert sources is not None, "proactive.default_enabled_sources missing from config"
        source_list = list(sources)
        # 7 baseline (3 core + 4 world-delta) + 4 warmth producers
        # (reengage_silence + late_night_dissolution removed 2026-06-09)
        # + reminder_fire (silent awareness, 2026-06-10)
        # - weirdly_good_mood_leak / irritation_event (demoted, Sprint 1 2026-07-02:
        #   no payload, failed the send-iff rule — contentless atmospherics).
        assert len(source_list) == 11
        assert "reminder_fire" in source_list
        assert "book_just_finished" in source_list
        assert "just_got_home" in source_list
        assert "weather_mood_shift" in source_list
        assert "weirdly_good_mood_leak" not in source_list
        assert "irritation_event" not in source_list
        for warm in (
            "anniversary_callback", "belief_resurface",
            "research_callback", "callback_episode",
        ):
            assert warm in source_list
