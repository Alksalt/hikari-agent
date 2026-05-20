"""Schedule resolver: figures out if the daily check-in should fire *now*.

Inputs: current local datetime, core_blocks.daily_checkin_schedule YAML,
runtime_state.daily_checkin_last_fired_date. Output: bool."""
from __future__ import annotations

import importlib
from datetime import datetime
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    monkeypatch.setenv("HOME_TZ", "Europe/Berlin")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from agents import config
    config.reload()
    yield


def _set_schedule(yaml_body: str) -> None:
    from storage import db
    db.upsert_core_block("daily_checkin_schedule", yaml_body)


def _at(local: str) -> datetime:
    """Build an aware datetime in Europe/Berlin from a 'YYYY-MM-DD HH:MM' string."""
    import zoneinfo
    return datetime.fromisoformat(local).replace(tzinfo=zoneinfo.ZoneInfo("Europe/Berlin"))


def test_default_time_fires_at_window():
    from agents.daily_checkin import should_fire_now
    _set_schedule('default_time: "07:00"\n')
    assert should_fire_now(_at("2026-05-21 07:00")) is True
    assert should_fire_now(_at("2026-05-21 07:04")) is True  # within poll window
    assert should_fire_now(_at("2026-05-21 06:55")) is False
    assert should_fire_now(_at("2026-05-21 07:06")) is False


def test_override_takes_priority_for_its_date():
    from agents.daily_checkin import should_fire_now
    _set_schedule(
        'default_time: "07:00"\n'
        'override_date: "2026-05-21"\n'
        'override_time: "14:30"\n'
    )
    assert should_fire_now(_at("2026-05-21 07:00")) is False  # override active
    assert should_fire_now(_at("2026-05-21 14:30")) is True
    assert should_fire_now(_at("2026-05-22 07:00")) is True   # override expired


def test_skip_date_blocks_fire():
    from agents.daily_checkin import should_fire_now
    _set_schedule(
        'default_time: "07:00"\n'
        'skip_dates: ["2026-05-21"]\n'
    )
    assert should_fire_now(_at("2026-05-21 07:00")) is False
    assert should_fire_now(_at("2026-05-22 07:00")) is True


def test_already_fired_today_blocks():
    from agents import daily_checkin
    from storage import db
    _set_schedule('default_time: "07:00"\n')
    db.runtime_set("daily_checkin_last_fired_date", "2026-05-21")
    assert daily_checkin.should_fire_now(_at("2026-05-21 07:00")) is False
    assert daily_checkin.should_fire_now(_at("2026-05-22 07:00")) is True


def test_missing_schedule_falls_back_to_default():
    """No core_block at all → use default_time from config."""
    from agents.daily_checkin import should_fire_now
    assert should_fire_now(_at("2026-05-21 07:00")) is True
    assert should_fire_now(_at("2026-05-21 09:00")) is False


def test_malformed_schedule_falls_back_to_default():
    from agents.daily_checkin import should_fire_now
    _set_schedule("this is not valid yaml: [unclosed")
    assert should_fire_now(_at("2026-05-21 07:00")) is True


def test_mark_fired_today_persists():
    from agents.daily_checkin import mark_fired_today
    from storage import db
    mark_fired_today(_at("2026-05-21 07:00"))
    assert db.runtime_get("daily_checkin_last_fired_date") == "2026-05-21"


def test_disabled_via_config(monkeypatch):
    """daily_checkin.enabled=false → never fires."""
    from agents import daily_checkin
    monkeypatch.setattr(daily_checkin, "_is_enabled", lambda: False)
    assert daily_checkin.should_fire_now(_at("2026-05-21 07:00")) is False
