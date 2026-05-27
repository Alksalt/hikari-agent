"""Tests for weather_fetch location fallback chain (Phase E).

Chain: explicit args → HOME_LAT/HOME_LON env → config weather.default_location.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

from agents import config
from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    config.reload()
    yield


@pytest.fixture()
def _config_with_default_location(tmp_path, monkeypatch):
    """Write a minimal engagement.yaml with weather.default_location set."""
    cfg_data = {
        "compound_turn": {"step_timeout_s": 12.0},
        "runtime": {
            "model_primary": "claude-sonnet-4-6",
            "model_fallback": "claude-sonnet-4-5",
        },
        "morning_brief": {"sources": ["open_meteo", "met_no"]},
        "weather": {
            "default_location": {
                "city": "Kristiansund",
                "lat": 63.111,
                "lon": 7.728,
            }
        },
    }
    cfg_path = tmp_path / "engagement.yaml"
    cfg_path.write_text(yaml.dump(cfg_data), encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(cfg_path))
    config.reload()
    yield
    config.reload()


def test_resolve_coords_uses_explicit_args():
    """Explicit non-zero lat/lon args win over env and config."""
    from tools.weather.fetch import _resolve_coords
    lat, lon, label = _resolve_coords({"lat": 59.91, "lon": 10.75, "label": "Oslo"})
    assert lat == pytest.approx(59.91)
    assert lon == pytest.approx(10.75)
    assert label == "Oslo"


def test_resolve_coords_env_wins_over_config(
    monkeypatch, _config_with_default_location
):
    """When lat/lon args are absent, HOME_LAT/HOME_LON env vars beat the config default."""
    monkeypatch.setenv("HOME_LAT", "59.91")
    monkeypatch.setenv("HOME_LON", "10.75")
    from tools.weather import fetch as fetch_mod
    importlib.reload(fetch_mod)

    from tools.weather.fetch import _resolve_coords
    lat, lon, label = _resolve_coords({})
    assert lat == pytest.approx(59.91)
    assert lon == pytest.approx(10.75)


def test_resolve_coords_falls_back_to_config_when_no_env(
    monkeypatch, _config_with_default_location
):
    """With HOME_LAT/HOME_LON unset and no args, config default location is used."""
    monkeypatch.delenv("HOME_LAT", raising=False)
    monkeypatch.delenv("HOME_LON", raising=False)
    from tools.weather import fetch as fetch_mod
    importlib.reload(fetch_mod)

    from tools.weather.fetch import _resolve_coords
    lat, lon, label = _resolve_coords({})
    assert lat == pytest.approx(63.111)
    assert lon == pytest.approx(7.728)
    assert label == "Kristiansund"


@pytest.mark.asyncio
async def test_weather_fetch_uses_config_location_when_no_env_no_args(
    monkeypatch, _config_with_default_location
):
    """End-to-end: with HOME_LAT/HOME_LON unset and no args, weather_fetch
    should call fetch_forecast with Kristiansund coordinates."""
    monkeypatch.delenv("HOME_LAT", raising=False)
    monkeypatch.delenv("HOME_LON", raising=False)

    calls: list[tuple[float, float]] = []

    async def fake_fetch(lat, lon):
        calls.append((lat, lon))
        return {
            "sources": {},
            "consensus": {"values": {}, "disagree": []},
            "windows": {},
            "sunrise": None,
            "sunset": None,
            "lat": lat,
            "lon": lon,
        }

    import tools.weather._shared as shared_mod
    from tools.weather import fetch as fetch_mod
    importlib.reload(fetch_mod)
    monkeypatch.setattr(shared_mod, "fetch_forecast", fake_fetch)
    monkeypatch.setattr(fetch_mod, "fetch_forecast", fake_fetch)

    await fetch_mod.weather_fetch.handler({})

    assert len(calls) == 1
    assert calls[0][0] == pytest.approx(63.111)
    assert calls[0][1] == pytest.approx(7.728)


@pytest.mark.asyncio
async def test_weather_fetch_env_coords_beat_config(
    monkeypatch, _config_with_default_location
):
    """When HOME_LAT/HOME_LON are set, they are used instead of the config default."""
    monkeypatch.setenv("HOME_LAT", "59.91")
    monkeypatch.setenv("HOME_LON", "10.75")

    calls: list[tuple[float, float]] = []

    async def fake_fetch(lat, lon):
        calls.append((lat, lon))
        return {
            "sources": {},
            "consensus": {"values": {}, "disagree": []},
            "windows": {},
            "sunrise": None,
            "sunset": None,
            "lat": lat,
            "lon": lon,
        }

    import tools.weather._shared as shared_mod
    from tools.weather import fetch as fetch_mod
    importlib.reload(fetch_mod)
    monkeypatch.setattr(shared_mod, "fetch_forecast", fake_fetch)
    monkeypatch.setattr(fetch_mod, "fetch_forecast", fake_fetch)

    await fetch_mod.weather_fetch.handler({})

    assert len(calls) == 1
    assert calls[0][0] == pytest.approx(59.91)
    assert calls[0][1] == pytest.approx(10.75)
