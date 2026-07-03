"""Tests for agents.engagement.producers.weather_mood_shift.

Covers two review fixes:
  - window selection must use the LOCAL hour, not datetime.now(UTC).hour —
    a non-UTC user's morning/midday/evening windows don't line up with UTC
    hour boundaries, so the wrong window (and wrong tag) gets picked.
  - emission must be gated on an actual weather_code/temp_c change, not just
    a different window slot being selected — a bucket flip alone (with the
    exact same underlying forecast data) is not a real weather transition.
"""
from __future__ import annotations

import importlib
import json
from datetime import UTC
from datetime import datetime as real_datetime
from pathlib import Path

import pytest

from agents.engagement.producers import weather_mood_shift as wms
from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from agents import config
    config.reload()
    yield


def _set_snapshot(snapshot: dict) -> None:
    db.runtime_set("weather_current_snapshot", json.dumps(snapshot))


# ---------------------------------------------------------------------------
# _local_hour: must read the configured local tz, not the host/UTC clock.
# ---------------------------------------------------------------------------

def test_local_hour_uses_configured_tz_not_utc(monkeypatch):
    monkeypatch.setattr(wms, "_resolve_local_tz_name", lambda: "Asia/Tokyo")

    class _Frozen(real_datetime):
        @classmethod
        def now(cls, tz=None):
            base = real_datetime(2026, 5, 20, 2, 0, 0, tzinfo=UTC)  # 02:00 UTC = 11:00 JST
            return base.astimezone(tz) if tz is not None else base

    monkeypatch.setattr(wms, "datetime", _Frozen)
    assert wms._local_hour() == 11, "must read 11:00 Tokyo, not 02:00 UTC"


def test_local_hour_falls_back_to_utc_on_bad_tz(monkeypatch):
    monkeypatch.setattr(wms, "_resolve_local_tz_name", lambda: "not/a/real/tz")
    # Should not raise — falls back to UTC hour.
    assert isinstance(wms._local_hour(), int)


# ---------------------------------------------------------------------------
# _condition_tag: window picked by the passed-in local hour.
# ---------------------------------------------------------------------------

def test_condition_tag_picks_window_by_local_hour():
    data = {
        "windows": {
            "morning": {"weather_code": 61, "temp_c": 12.0},   # rain
            "midday": {"weather_code": 0, "temp_c": 20.0},     # nothing notable
            "evening": {"weather_code": 0, "temp_c": 29.0},    # hot
        },
        "consensus": {},
    }
    tag_morning, fp_morning = wms._condition_tag(data, 9)   # 7<=9<12 -> morning
    assert tag_morning == "rain"
    assert fp_morning == [61, 12.0]

    tag_midday, _ = wms._condition_tag(data, 14)  # else-bucket -> midday
    assert tag_midday is None

    tag_evening, fp_evening = wms._condition_tag(data, 19)  # evening
    assert tag_evening == "hot"
    assert fp_evening == [0, 29.0]


# ---------------------------------------------------------------------------
# collect(): fingerprint gate suppresses window-slot-only flips.
# ---------------------------------------------------------------------------

def test_window_flip_without_data_change_does_not_fire(monkeypatch):
    """Same weather_code/temp_c duplicated across window buckets: a bucket
    flip (driven only by local hour advancing) must not fire — the label
    changes (rain -> rain_evening) even though the underlying data didn't."""
    snapshot = {
        "windows": {
            "morning": {"weather_code": 61, "temp_c": 10.0},
            "midday": {"weather_code": 61, "temp_c": 10.0},
            "evening": {"weather_code": 61, "temp_c": 10.0},
        },
        "consensus": {},
    }
    _set_snapshot(snapshot)

    monkeypatch.setattr(wms, "_local_hour", lambda: 13)  # midday
    assert wms.collect() == []  # first observation — records baseline, no fire

    monkeypatch.setattr(wms, "_local_hour", lambda: 18)  # evening — label flips
    assert wms.collect() == [], (
        "must not fire on a window-slot flip with unchanged code/temp"
    )


def test_real_data_change_still_fires(monkeypatch):
    """A genuine forecast change (different code/temp) must still fire —
    the fingerprint gate must not swallow real transitions."""
    snapshot1 = {
        "windows": {
            "morning": {"weather_code": 61, "temp_c": 10.0},
            "midday": {"weather_code": 61, "temp_c": 10.0},
            "evening": {"weather_code": 61, "temp_c": 10.0},
        },
        "consensus": {},
    }
    _set_snapshot(snapshot1)
    monkeypatch.setattr(wms, "_local_hour", lambda: 13)
    assert wms.collect() == []  # first observation

    snapshot2 = {
        "windows": {
            "morning": {"weather_code": 61, "temp_c": 10.0},
            "midday": {"weather_code": 0, "temp_c": 30.0},
            "evening": {"weather_code": 0, "temp_c": 30.0},
        },
        "consensus": {},
    }
    _set_snapshot(snapshot2)
    candidates = wms.collect()
    assert len(candidates) == 1
    assert candidates[0].payload["to_condition"] == "hot"
    assert candidates[0].payload["from_condition"] == "rain"


def test_mark_consumed_persists_tag_and_fingerprint(monkeypatch):
    from datetime import UTC, datetime, timedelta

    from agents.engagement.triggers import TriggerCandidate

    candidate = TriggerCandidate(
        source="weather_mood_shift",
        pool="agent_spontaneous",
        pattern="notify",
        novelty=0.5,
        actionability=0.5,
        confidence=0.5,
        payload={
            "from_condition": "rain",
            "to_condition": "hot",
            "_fingerprint": [0, 30.0],
        },
        dedup_key="k",
        decay_at=datetime.now(UTC) + timedelta(hours=5),
    )
    wms.mark_consumed(candidate)
    stored = json.loads(db.runtime_get(wms._LAST_CONDITION_KEY))
    assert stored == {"tag": "hot", "fingerprint": [0, 30.0]}

    # Next collect() with unchanged data (matching the just-persisted state)
    # must not re-fire.
    _set_snapshot({
        "windows": {"morning": {"weather_code": 0, "temp_c": 30.0},
                    "midday": {"weather_code": 0, "temp_c": 30.0},
                    "evening": {"weather_code": 0, "temp_c": 30.0}},
        "consensus": {},
    })
    monkeypatch.setattr(wms, "_local_hour", lambda: 13)
    assert wms.collect() == []
