"""Tests for agents/daily_brief.py — section collectors + weather notability
gate (Sprint 1, Task 2).

Uses a local ``fresh_db`` fixture (the repo has no shared fixture of that
name in conftest.py; mirrors the reload + _reset_schema_sentinel pattern
established in tests/test_proactive_backoff.py / tests/test_schema_constraints.py).
"""
from __future__ import annotations

import importlib

import pytest

from agents import daily_brief
from storage import db


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield db
    db._reset_schema_sentinel()


def _forecast(rain=10, high=15.0, code=3, wind=20):
    return {
        "consensus": {"values": {
            "temp_high_c": high, "temp_low_c": 8.0,
            "precip_prob_max_pct": rain, "wind_max_kmh": wind,
            "feels_high_c": high, "feels_low_c": 7.0, "uv_index_max": 2,
        }, "disagree": []},
        "sources": {"open-meteo": {}},
        "windows": {"morning": {"temp_c": 10, "weather_code": code,
                                "precip_prob_pct": rain},
                    "midday": {}, "evening": {}},
    }


def test_weather_notable_on_rain_threshold():
    notable, reasons = daily_brief._weather_notable(_forecast(rain=80), _forecast(rain=80))
    assert notable and any("rain" in r for r in reasons)


def test_weather_not_notable_when_boring_and_unchanged():
    notable, _ = daily_brief._weather_notable(_forecast(rain=10), _forecast(rain=10))
    assert not notable


def test_weather_notable_on_temp_delta():
    notable, reasons = daily_brief._weather_notable(
        _forecast(rain=10, high=24.0), _forecast(rain=10, high=15.0))
    assert notable and any("temp" in r for r in reasons)


def test_weather_notable_when_no_previous_snapshot():
    # first run after deploy: send it (no baseline to compare against)
    notable, _ = daily_brief._weather_notable(_forecast(rain=10), None)
    assert notable


@pytest.mark.asyncio
async def test_collect_sections_empty_everything(monkeypatch, fresh_db):
    async def no_email():
        return {"unread_personal": [], "calendar_invites": [],
                "deletable": {"count": 0, "top_senders": [], "sample_ids": []}}
    async def no_events():
        return []
    monkeypatch.setattr(daily_brief, "fetch_email_buckets", no_email)
    monkeypatch.setattr(daily_brief, "fetch_calendar_events", no_events)
    monkeypatch.setattr(daily_brief, "_resolve_location", lambda: None)
    sections = await daily_brief.collect_sections()
    assert sections["email"] is None
    assert sections["calendar"] is None
    assert sections["weather"] is None


@pytest.mark.asyncio
async def test_collect_sections_email_present_when_only_deletable_nonzero(monkeypatch, fresh_db):
    """A deletable-only inbox (no unread personal, no invites) still earns an
    email section — deletable.count > 0 alone is signal."""
    async def deletable_only_email():
        return {"unread_personal": [], "calendar_invites": [],
                "deletable": {"count": 3, "top_senders": ["promo@x.com"], "sample_ids": []}}
    async def no_events():
        return []
    monkeypatch.setattr(daily_brief, "fetch_email_buckets", deletable_only_email)
    original_get = daily_brief.cfg.get
    monkeypatch.setattr(
        daily_brief.cfg, "get",
        lambda key, default=None: True
        if key == "daily_brief.include_generic_email"
        else original_get(key, default),
    )
    monkeypatch.setattr(daily_brief, "fetch_calendar_events", no_events)
    monkeypatch.setattr(daily_brief, "_resolve_location", lambda: None)
    sections = await daily_brief.collect_sections()
    assert sections["email"] is not None
    assert sections["email"]["deletable"]["count"] == 3
    assert sections["calendar"] is None
    assert sections["weather"] is None


@pytest.mark.asyncio
async def test_collect_sections_dedicated_mailbox_never_queries_generic_email(monkeypatch, fresh_db):
    async def must_not_fetch():
        raise AssertionError("generic Gmail buckets must stay off for the dedicated mailbox")

    async def no_events():
        return []

    original_get = daily_brief.cfg.get
    monkeypatch.setattr(
        daily_brief.cfg, "get",
        lambda key, default=None: False
        if key == "daily_brief.include_generic_email"
        else original_get(key, default),
    )
    monkeypatch.setattr(daily_brief, "fetch_email_buckets", must_not_fetch)
    monkeypatch.setattr(daily_brief, "fetch_calendar_events", no_events)
    monkeypatch.setattr(daily_brief, "_resolve_location", lambda: None)
    sections = await daily_brief.collect_sections()
    assert sections["email"] is None
