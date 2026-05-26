"""Tests for the weather_fetch tool envelope shape (presentation_hint, sources, data, notes)."""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

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


def _make_source(temp_high: float, temp_low: float) -> dict:
    return {
        "temp_high_c": temp_high,
        "temp_low_c": temp_low,
        "feels_high_c": temp_high - 2,
        "feels_low_c": temp_low - 2,
        "precip_prob_max_pct": 30,
        "weather_code_daily": 3,
        "wind_max_kmh": 10,
        "uv_index_max": 3,
        "sunrise": "2026-05-22T05:00",
        "sunset": "2026-05-22T21:00",
        "date": "2026-05-22",
        "hourly": {
            "time": [f"2026-05-22T0{h}:00" for h in range(7, 10)],
            "temp_c": [temp_high - 3.0] * 3,
            "feels_c": [temp_high - 5.0] * 3,
            "precip_prob_pct": [20] * 3,
            "weather_code": [2] * 3,
            "cloud_cover_pct": [40] * 3,
        },
    }


def _make_forecast(source_a_high: float = 18.0, source_b_high: float = 18.0) -> dict:
    from tools.weather._sources import _consensus
    sources = {
        "open_meteo": _make_source(source_a_high, 10.0),
        "met_no": _make_source(source_b_high, 11.0),
    }
    return {"sources": sources, "consensus": _consensus(sources), "lat": 59.91, "lon": 10.75}


async def _call_weather_fetch(monkeypatch, source_a_high: float = 18.0, source_b_high: float = 18.0) -> dict:
    """Call weather_fetch via its .handler, patching fetch_forecast to avoid network."""
    async def fake_fetch(lat, lon):
        return _make_forecast(source_a_high, source_b_high)

    # Patch BOTH the source module (for any indirect callers) AND the caller-side
    # binding in tools.weather.fetch — `fetch.py` does `from ... import fetch_forecast`
    # at module load, so patching only _shared.fetch_forecast leaves the bound name
    # in fetch.py pointing at the real (network-hitting) function.
    import tools.weather._shared as shared_mod
    from tools.weather import fetch as fetch_mod
    monkeypatch.setattr(shared_mod, "fetch_forecast", fake_fetch)
    monkeypatch.setattr(fetch_mod, "fetch_forecast", fake_fetch)
    return await fetch_mod.weather_fetch.handler({"lat": 59.91, "lon": 10.75, "label": "Oslo"})


@pytest.mark.asyncio
async def test_envelope_includes_presentation_hint(monkeypatch):
    result = await _call_weather_fetch(monkeypatch)
    text = result["content"][0]["text"]
    assert "### presentation_hint\nweather_three_window" in text


@pytest.mark.asyncio
async def test_envelope_includes_sources_section(monkeypatch):
    result = await _call_weather_fetch(monkeypatch)
    text = result["content"][0]["text"]
    assert "### sources" in text
    assert "- open_meteo" in text
    assert "- met_no" in text


@pytest.mark.asyncio
async def test_envelope_includes_data_json_fence(monkeypatch):
    result = await _call_weather_fetch(monkeypatch)
    text = result["content"][0]["text"]
    assert "### data\n```json" in text
    json_start = text.index("### data\n```json\n") + len("### data\n```json\n")
    json_end = text.index("\n```", json_start)
    parsed = json.loads(text[json_start:json_end])
    assert "consensus" in parsed
    assert "windows" in parsed


@pytest.mark.asyncio
async def test_envelope_includes_notes_when_disagreement(monkeypatch):
    # 4°C spread on temp_high triggers disagreement
    result = await _call_weather_fetch(monkeypatch, source_a_high=15.0, source_b_high=19.0)
    text = result["content"][0]["text"]
    assert "### notes" in text
    assert "temp_high_c" in text


@pytest.mark.asyncio
async def test_envelope_data_still_set_on_dict(monkeypatch):
    result = await _call_weather_fetch(monkeypatch)
    assert result.get("data") is not None
    assert "consensus" in result["data"]
