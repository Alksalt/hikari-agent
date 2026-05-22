"""Phase 10: multi-source weather forecast merge."""
from __future__ import annotations
import importlib
from pathlib import Path
import pytest
from storage import db
from agents import config

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


class _FakeResponse:
    def __init__(self, status_code: int, json_data: dict | None = None):
        self.status_code = status_code
        self._json = json_data or {}
    def json(self): return self._json
    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class _FakeAsyncClient:
    def __init__(self, *args, **kwargs): self.responses: dict[str, _FakeResponse] = {}
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, url, **kwargs):
        for prefix, resp in self.responses.items():
            if url.startswith(prefix): return resp
        raise Exception(f"no mock for {url}")


@pytest.mark.asyncio
async def test_fetch_forecast_merges_two_sources(monkeypatch):
    from tools import weather
    import httpx
    client = _FakeAsyncClient()
    client.responses["https://api.open-meteo.com"] = _FakeResponse(200, {
        "daily": {
            "time": ["2026-05-19"],
            "temperature_2m_max": [18.0],
            "temperature_2m_min": [10.0],
            "weather_code": [3],
            "precipitation_probability_max": [40],
        }
    })
    client.responses["https://api.met.no"] = _FakeResponse(200, {
        "properties": {"timeseries": [
            {"time": "2026-05-19T12:00:00Z", "data": {"instant": {"details": {
                "air_temperature": 17.0
            }}, "next_6_hours": {"summary": {"symbol_code": "cloudy"}}}},
        ]}
    })
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: client)
    out = await weather.fetch_forecast(59.91, 10.75)
    assert "sources" in out
    assert "open_meteo" in out["sources"]
    assert "met_no" in out["sources"]
    assert out["consensus"]["values"]["temp_high_c"] is not None


@pytest.mark.asyncio
async def test_fetch_forecast_partial_failure_still_returns(monkeypatch):
    from tools import weather
    import httpx
    client = _FakeAsyncClient()
    client.responses["https://api.open-meteo.com"] = _FakeResponse(200, {
        "daily": {"time": ["2026-05-19"], "temperature_2m_max": [18.0],
                  "temperature_2m_min": [10.0], "weather_code": [3],
                  "precipitation_probability_max": [40]}
    })
    client.responses["https://api.met.no"] = _FakeResponse(500)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: client)
    out = await weather.fetch_forecast(59.91, 10.75)
    assert "open_meteo" in out["sources"]
    assert "met_no" not in out["sources"]
