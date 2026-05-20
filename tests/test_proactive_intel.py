"""Stage-3 proactive intelligence tests: affect decay, cadence governor,
soft-scarcity, observations, noticings."""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agents import affect, cadence, config
from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    config.reload()
    affect.reload_patterns()
    yield


# ---------- emotional half-life ----------

def test_affect_scan_records_heavy_moment():
    triggered, matched = affect.scan_inbound("i think my grandma died last night")
    assert triggered
    assert matched
    state = affect.current_affect()
    assert state is not None
    assert state["intensity"] > 0.9  # fresh = near 1.0
    assert state["kind"] in ("raw", "quiet", "sharp", "tired", "soft")


def test_affect_scan_no_false_positive():
    triggered, matched = affect.scan_inbound("today was fine. nothing much to report.")
    assert not triggered
    assert matched is None
    assert affect.current_affect() is None


def test_affect_decay_drops_intensity_below_threshold(monkeypatch, tmp_path):
    """After enough hours, intensity should fall below the inject threshold.
    Pin decay_hours so the test doesn't break if the config default changes."""
    cfg_text = (
        "emotional_half_life:\n"
        "  enabled: true\n"
        "  decay_hours: 12\n"
        "  min_intensity_to_inject: 0.15\n"
        "  heavy_moment_signals: []\n"
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()
    # Plant a state with a stale timestamp.
    stale = {
        "last_heavy_at": (datetime.now(UTC) - timedelta(hours=72)).isoformat(),
        "intensity": 1.0,
        "kind": "raw",
    }
    db.runtime_set("affect_state", json.dumps(stale))
    state = affect.current_affect()
    assert state is not None
    # decay_hours=12 → after 72h intensity is ~1.0 * 0.5**6 = 0.015 < 0.05
    assert state["intensity"] < 0.05


def test_affect_inject_block_empty_when_no_state():
    assert affect.inject_affect_block() == ""


def test_affect_inject_block_renders_when_fresh():
    affect.scan_inbound("she broke up with me yesterday")
    out = affect.inject_affect_block()
    assert out, "fresh state should produce an inject block"
    assert "decayed" in out.lower() or "heavy" in out.lower()


# ---------- cadence governor ----------

def test_cadence_count_starts_zero():
    assert cadence.proactive_count_last_7d() == 0


def test_cadence_record_appends_and_caps_window():
    cadence.record_proactive_sent()
    cadence.record_proactive_sent()
    assert cadence.proactive_count_last_7d() == 2


def test_cadence_prunes_old_entries_outside_window():
    # Manually plant entries older than 7 days.
    old_iso = (datetime.now(UTC) - timedelta(days=14)).isoformat()
    fresh_iso = datetime.now(UTC).isoformat()
    db.runtime_set("proactive_log_v1", json.dumps([old_iso, fresh_iso]))
    assert cadence.proactive_count_last_7d() == 1  # old one dropped


def test_cadence_governor_blocks_at_cap(monkeypatch, tmp_path):
    cfg_text = (
        "cadence_governor:\n"
        "  enabled: true\n"
        "  max_proactive_per_7d: 2\n"
        "  allowed_trigger_sources: [open_loop, recent_episode_callback]\n"
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()
    cadence.record_proactive_sent()
    cadence.record_proactive_sent()
    allowed, reason = cadence.can_send_proactive("open_loop")
    assert not allowed
    assert "cap_reached" in reason


def test_cadence_governor_blocks_unjustified_source(monkeypatch, tmp_path):
    cfg_text = (
        "cadence_governor:\n"
        "  enabled: true\n"
        "  max_proactive_per_7d: 10\n"
        "  allowed_trigger_sources: [open_loop, pattern_observation]\n"
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()
    allowed, reason = cadence.can_send_proactive("recent_episode_callback")
    assert not allowed
    assert "source_not_justified" in reason


def test_cadence_governor_allows_justified_under_cap(monkeypatch, tmp_path):
    cfg_text = (
        "cadence_governor:\n"
        "  enabled: true\n"
        "  max_proactive_per_7d: 4\n"
        "  allowed_trigger_sources: [open_loop, pattern_observation, reengage_silence]\n"
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()
    allowed, reason = cadence.can_send_proactive("open_loop")
    assert allowed
    assert reason == "ok"


# ---------- observations ----------

def test_observation_record_dedupes_by_signature():
    aid = db.observation_record("recurrence", "11pm-quiet", "goes quiet near 11pm", 0.7)
    bid = db.observation_record("recurrence", "11pm-quiet",
                                "goes quiet near 11pm again, confirmed", 0.85)
    assert aid == bid
    rows = db.observations_unsurfaced(min_confidence=0.6, limit=5)
    assert len(rows) == 1
    assert rows[0]["confidence"] >= 0.85  # confidence updated on conflict


def test_observation_unsurfaced_filter():
    db.observation_record("topic_pattern", "low-conf", "weak signal", 0.3)
    db.observation_record("topic_pattern", "high-conf", "strong signal", 0.9)
    rows = db.observations_unsurfaced(min_confidence=0.6, limit=5)
    assert len(rows) == 1
    assert rows[0]["signature"] == "high-conf"


def test_observation_mark_surfaced_hides_until_re_surface_window():
    oid = db.observation_record("recurrence", "sig", "summary", 0.9)
    db.observation_mark_surfaced(oid)
    # With re_surface_min_days=7, freshly-surfaced obs is hidden.
    rows = db.observations_unsurfaced(min_confidence=0.6, re_surface_min_days=7)
    assert rows == []
    # But with re_surface_min_days=0, it shows again.
    rows = db.observations_unsurfaced(min_confidence=0.6, re_surface_min_days=0)
    assert len(rows) == 1


# ---------- noticings ----------

def test_noticing_record_and_consume():
    nid = db.noticing_record(
        "topic_dropped", "you stopped mentioning the side project.",
        short_value=0.0, long_value=4.0,
    )
    rows = db.noticings_unsurfaced(limit=5)
    assert any(r["id"] == nid for r in rows)
    db.noticing_mark_surfaced(nid)
    assert db.noticings_unsurfaced(limit=5) == []


def test_noticing_prune_old():
    # Insert one current and one ancient.
    db.noticing_record("shift", "current one")
    with db._conn() as c:
        ancient = (datetime.now(UTC) - timedelta(days=120)).isoformat()
        c.execute(
            "INSERT INTO noticings (signal, summary, created_at) VALUES (?, ?, ?)",
            ("shift", "ancient", ancient),
        )
    deleted = db.prune_noticings_older_than_days(60)
    assert deleted == 1


# ---------- integration: cadence governor blocks the real heartbeat path ----------

@pytest.mark.asyncio
async def test_heartbeat_blocked_when_cap_reached(monkeypatch, tmp_path):
    """Wires the full maybe_send_heartbeat path against a cap-saturated governor
    and verifies no message is sent and no LLM call is made."""
    cfg_text = (
        "proactive:\n"
        "  heartbeat_min_interval_hours: 0\n"
        "  heartbeat_max_interval_hours: 8\n"
        "  quiet_start_hour: 23\n"
        "  quiet_end_hour: 8\n"
        "  user_active_skip_minutes: 60\n"
        "  reengage_min_hours: 2\n"
        "  reengage_max_hours: 6\n"
        "cadence_governor:\n"
        "  enabled: true\n"
        "  max_proactive_per_7d: 1\n"
        "  allowed_trigger_sources: [open_loop, recent_episode_callback,\n"
        "    pattern_observation, noticed_change, lexicon_callback, reengage_silence]\n"
        "soft_scarcity:\n"
        "  enabled: false\n"
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()

    # Saturate the cap.
    cadence.record_proactive_sent()

    # Stub run_proactive so a wiring bug would make a real call (and we'd see it).
    sent_calls: list[str] = []
    run_proactive_calls: list[str] = []

    from agents import proactive

    async def fake_run_proactive(prompt: str) -> str:
        run_proactive_calls.append(prompt)
        return "should never ship"

    async def fake_send(text: str) -> None:
        sent_calls.append(text)

    monkeypatch.setattr(proactive, "run_proactive", fake_run_proactive)
    # _pick_seed needs templates to exist; provide a minimal stub.
    monkeypatch.setattr(proactive, "_load_templates", lambda: [(1, "stub seed")])

    # Avoid the quiet-hours and user-active-skip gates by clearing those keys.
    db.runtime_set("last_user_message", None)
    db.runtime_set("last_proactive_sent", None)

    sent = await proactive.maybe_send_heartbeat(fake_send)
    assert not sent, "governor should have blocked"
    assert sent_calls == []
    # Crucially: the cap check should run BEFORE the LLM call.
    assert run_proactive_calls == [], "should not have invoked LLM under cap"


# ---------- calendar heartbeat ----------

@pytest.mark.asyncio
async def test_calendar_heartbeat_disabled_returns_false(monkeypatch, tmp_path):
    """When calendar_heartbeat.enabled=false, do not call run_proactive at all."""
    cfg_text = (
        "calendar_heartbeat:\n"
        "  enabled: false\n"
        "  lookahead_minutes: 120\n"
        "  prep_message_lead_minutes: 30\n"
        "  lead_window_jitter_minutes: 5\n"
        "  min_event_duration_minutes: 15\n"
        "  exclude_calendar_ids: []\n"
        "  scheduler_interval_minutes: 5\n"
        "  notified_ttl_hours: 4\n"
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()

    from agents import proactive

    run_calls: list[str] = []

    async def fake_run_proactive(prompt: str) -> str:
        run_calls.append(prompt)
        return ""

    monkeypatch.setattr(proactive, "run_proactive", fake_run_proactive)

    async def fake_send(text: str) -> None:
        raise AssertionError("send_text must not be called when disabled")

    sent = await proactive.maybe_send_calendar_heartbeat(fake_send)
    assert sent is False
    assert run_calls == []


def test_calendar_event_signature_stable():
    from agents import proactive

    ev = {
        "id": "abc123",
        "title": "Standup",
        "start_iso": "2026-05-19T09:00:00+00:00",
        "end_iso": "2026-05-19T09:30:00+00:00",
    }
    sig_a = proactive._calendar_event_signature(ev)
    sig_b = proactive._calendar_event_signature(dict(ev))
    assert sig_a == sig_b

    other = dict(ev)
    other["start_iso"] = "2026-05-19T10:00:00+00:00"
    assert proactive._calendar_event_signature(other) != sig_a

    diff_title = dict(ev)
    diff_title["title"] = "Different"
    assert proactive._calendar_event_signature(diff_title) != sig_a

    diff_id = dict(ev)
    diff_id["id"] = "xyz789"
    assert proactive._calendar_event_signature(diff_id) != sig_a


def test_calendar_event_dedup():
    from agents import proactive

    sig = "abc|2026-05-19T09:00:00+00:00|Standup"
    assert not proactive._calendar_event_already_notified(sig)
    proactive._mark_calendar_event_notified(sig)
    assert proactive._calendar_event_already_notified(sig)


@pytest.mark.asyncio
async def test_calendar_heartbeat_skips_already_notified(monkeypatch, tmp_path):
    """If an event in the lead window is already in the notified set, skip silently."""
    cfg_text = (
        "calendar_heartbeat:\n"
        "  enabled: true\n"
        "  lookahead_minutes: 120\n"
        "  prep_message_lead_minutes: 30\n"
        "  lead_window_jitter_minutes: 5\n"
        "  min_event_duration_minutes: 15\n"
        "  exclude_calendar_ids: []\n"
        "  scheduler_interval_minutes: 5\n"
        "  notified_ttl_hours: 4\n"
        "cadence_governor:\n"
        "  enabled: true\n"
        "  max_proactive_per_7d: 10\n"
        "  allowed_trigger_sources: [calendar_event]\n"
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()

    from agents import proactive

    now = datetime.now(UTC)
    start = now + timedelta(minutes=30)
    end = start + timedelta(minutes=30)
    event = {
        "id": "evt-1",
        "title": "Standup",
        "start_iso": start.isoformat(),
        "end_iso": end.isoformat(),
    }

    async def fake_fetch(lookahead_minutes: int):
        return [event]

    monkeypatch.setattr(proactive, "_fetch_upcoming_events", fake_fetch)

    # Pre-mark the event as notified.
    proactive._mark_calendar_event_notified(
        proactive._calendar_event_signature(event)
    )

    run_calls: list[str] = []

    async def fake_run_proactive(prompt: str) -> str:
        run_calls.append(prompt)
        return "should not ship"

    monkeypatch.setattr(proactive, "run_proactive", fake_run_proactive)

    async def fake_send(text: str) -> None:
        raise AssertionError("send_text must not be called for a dedup'd event")

    sent = await proactive.maybe_send_calendar_heartbeat(fake_send)
    assert sent is False
    assert run_calls == [], "no LLM call should fire for an already-notified event"


@pytest.mark.asyncio
async def test_calendar_heartbeat_fires_for_fresh_event(monkeypatch, tmp_path):
    """A fresh event in the lead window: cadence allows, message sends, dedup marked."""
    cfg_text = (
        "calendar_heartbeat:\n"
        "  enabled: true\n"
        "  lookahead_minutes: 120\n"
        "  prep_message_lead_minutes: 30\n"
        "  lead_window_jitter_minutes: 5\n"
        "  min_event_duration_minutes: 15\n"
        "  exclude_calendar_ids: []\n"
        "  scheduler_interval_minutes: 5\n"
        "  notified_ttl_hours: 4\n"
        "cadence_governor:\n"
        "  enabled: true\n"
        "  max_proactive_per_7d: 10\n"
        "  allowed_trigger_sources: [calendar_event]\n"
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()

    from agents import proactive

    now = datetime.now(UTC)
    start = now + timedelta(minutes=30)
    end = start + timedelta(minutes=30)
    event = {
        "id": "evt-fresh",
        "title": "Deep work block",
        "start_iso": start.isoformat(),
        "end_iso": end.isoformat(),
    }

    async def fake_fetch(lookahead_minutes: int):
        return [event]

    monkeypatch.setattr(proactive, "_fetch_upcoming_events", fake_fetch)

    run_calls: list[str] = []

    async def fake_run_proactive(prompt: str) -> str:
        run_calls.append(prompt)
        return "30 in. mic on, lights on. don't make me chase you."

    monkeypatch.setattr(proactive, "run_proactive", fake_run_proactive)

    sent_texts: list[str] = []

    async def fake_send(text: str) -> None:
        sent_texts.append(text)

    count_before = cadence.proactive_count_last_7d()
    sent = await proactive.maybe_send_calendar_heartbeat(fake_send)
    assert sent is True
    assert len(sent_texts) == 1
    assert sent_texts[0].startswith("30 in.")
    # Dedup marker was written.
    sig = proactive._calendar_event_signature(event)
    assert proactive._calendar_event_already_notified(sig)
    # Cadence ledger incremented.
    assert cadence.proactive_count_last_7d() == count_before + 1
    # last_proactive_sent stamp updated.
    assert db.runtime_get("last_proactive_sent")
    # Generation call did happen (prompt was constructed).
    assert len(run_calls) == 1
    # A second call (event still in window, but marker present) is a no-op.
    sent_again = await proactive.maybe_send_calendar_heartbeat(fake_send)
    assert sent_again is False
    assert len(sent_texts) == 1


# ---------- T7.2: recurring-location detection ----------

def test_detect_recurring_location_pattern_finds_repeat():
    """Four photos at the same coords + two elsewhere → returns the repeat."""
    for _ in range(4):
        db.photo_location_insert(59.91, 10.75, label="oslo cafe")
    db.photo_location_insert(40.0, 10.0)
    db.photo_location_insert(50.0, 20.0)
    from agents.proactive import detect_recurring_location_pattern

    result = detect_recurring_location_pattern(window_days=7, min_visits=3)
    assert result is not None
    assert result["visit_count"] >= 4
    assert round(result["lat"], 3) == 59.910
    assert round(result["lon"], 3) == 10.750
    assert result["label"] == "oslo cafe"


def test_detect_recurring_location_pattern_returns_none_when_no_repeats():
    db.photo_location_insert(59.91, 10.75)
    db.photo_location_insert(40.0, 10.0)
    from agents.proactive import detect_recurring_location_pattern

    assert detect_recurring_location_pattern() is None


def test_detect_recurring_location_pattern_respects_window():
    """A spot visited 5 times but all >7 days ago should not be returned."""
    # Insert five rows then backdate them all to two weeks ago.
    ids: list[int] = []
    for _ in range(5):
        ids.append(db.photo_location_insert(45.0, 5.0, label="old place"))
    backdate_iso = (datetime.now(UTC) - timedelta(days=14)).isoformat()
    with db._conn() as conn:
        for row_id in ids:
            conn.execute(
                "UPDATE photo_locations SET received_at = ? WHERE id = ?",
                (backdate_iso, row_id),
            )
    from agents.proactive import detect_recurring_location_pattern

    assert detect_recurring_location_pattern(window_days=7, min_visits=3) is None


def test_photo_locations_recent_orders_newest_first():
    db.photo_location_insert(1.0, 1.0, label="first")
    db.photo_location_insert(2.0, 2.0, label="second")
    rows = db.photo_locations_recent(limit=5)
    assert len(rows) == 2
    # Most recent insertion first.
    assert rows[0]["label"] == "second"
    assert rows[1]["label"] == "first"
