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
    cadence.record_spontaneous_sent("open_loop")
    cadence.record_spontaneous_sent("open_loop")
    assert cadence.proactive_count_last_7d() == 2


def test_cadence_prunes_old_entries_outside_window():
    # Manually plant entries older than 7 days.
    old_iso = (datetime.now(UTC) - timedelta(days=14)).isoformat()
    fresh_iso = datetime.now(UTC).isoformat()
    db.runtime_set("proactive_log_v1", json.dumps([old_iso, fresh_iso]))
    assert cadence.proactive_count_last_7d() == 1  # old one dropped


def test_cadence_governor_blocks_at_cap(monkeypatch, tmp_path):
    # Pool-based YAML: open_loop is in agent_spontaneous, cap=2 for the test.
    cfg_text = (
        "cadence_governor:\n"
        "  enabled: true\n"
        "  pools:\n"
        "    agent_spontaneous:\n"
        "      max_per_7d: 2\n"
        "      allowed_sources: [open_loop, reengage_silence]\n"
        "    scheduled_ceremony:\n"
        "      max_per_7d: 14\n"
        "      allowed_sources: [daily_checkin]\n"
        "    user_anchored:\n"
        "      max_per_7d: 30\n"
        "      allowed_sources: [wiki_new_file]\n"
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()
    cadence.record_spontaneous_sent("open_loop")
    cadence.record_spontaneous_sent("open_loop")
    allowed, reason = cadence.can_send("open_loop", cadence.Pool.AGENT_SPONTANEOUS)
    assert not allowed
    assert "cap_reached" in reason


def test_cadence_governor_blocks_unjustified_source(monkeypatch, tmp_path):
    # recent_episode_callback is NOT in any pool in this reduced config.
    cfg_text = (
        "cadence_governor:\n"
        "  enabled: true\n"
        "  pools:\n"
        "    agent_spontaneous:\n"
        "      max_per_7d: 10\n"
        "      allowed_sources: [open_loop, pattern_observation]\n"
        "    scheduled_ceremony:\n"
        "      max_per_7d: 14\n"
        "      allowed_sources: [daily_checkin]\n"
        "    user_anchored:\n"
        "      max_per_7d: 30\n"
        "      allowed_sources: [wiki_new_file]\n"
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()
    allowed, reason = cadence.can_send("recent_episode_callback")
    assert not allowed
    assert "source_not_justified" in reason


def test_cadence_governor_allows_justified_under_cap(monkeypatch, tmp_path):
    cfg_text = (
        "cadence_governor:\n"
        "  enabled: true\n"
        "  pools:\n"
        "    agent_spontaneous:\n"
        "      max_per_7d: 4\n"
        "      allowed_sources: [open_loop, pattern_observation, reengage_silence]\n"
        "    scheduled_ceremony:\n"
        "      max_per_7d: 14\n"
        "      allowed_sources: [daily_checkin]\n"
        "    user_anchored:\n"
        "      max_per_7d: 30\n"
        "      allowed_sources: [wiki_new_file]\n"
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()
    allowed, reason = cadence.can_send("open_loop", cadence.Pool.AGENT_SPONTANEOUS)
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
