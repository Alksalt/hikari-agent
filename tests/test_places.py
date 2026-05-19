"""Phase 10: places_search + place_open_now via OSM Overpass."""
from __future__ import annotations
import importlib
from pathlib import Path
import pytest
from storage import db
from agents import config

@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
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
    def __init__(self, status_code, json_data=None):
        self.status_code = status_code; self._json = json_data or {}
    def json(self): return self._json
    def raise_for_status(self):
        if self.status_code >= 400: raise Exception(f"HTTP {self.status_code}")


class _FakeAsyncClient:
    def __init__(self, *a, **k): self.responses = {}
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, **kwargs):
        for prefix, resp in self.responses.items():
            if url.startswith(prefix): return resp
        raise Exception(f"no mock for {url}")


@pytest.mark.asyncio
async def test_places_search_rejects_overpass_qli_chars(monkeypatch):
    """R1 finding: user-supplied query was interpolated raw into Overpass QL.
    Sanitizer must strip ", ], ;, \\, newlines so an attacker can't escape
    the regex literal and inject arbitrary QL."""
    from tools import places
    import httpx
    captured: dict = {}

    class _CapturedClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, **kwargs):
            captured["data"] = kwargs.get("data", {}).get("data", "")
            return _FakeResponse(200, {"elements": []})

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _CapturedClient())
    await places.places_search.handler({
        "query": '"]; out; injected~"', "lat": 59.91, "lon": 10.75, "radius_m": 500,
    })
    body = captured["data"]
    # The dangerous chars from user input must NOT appear in the QL body.
    # ", ], \ would close the regex literal; ; would inject a new statement.
    # Template legitimately uses these (e.g. `[out:json][timeout:15];`), so we
    # check the per-line user-substituted slots — content between the
    # surrounding quotes of name/amenity/shop tags.
    import re as _re
    for m in _re.finditer(r'node\["[^"]+"[~=]"([^"]*)"', body):
        substituted = m.group(1)
        assert '"' not in substituted
        assert ']' not in substituted
        assert ';' not in substituted
        assert '\\' not in substituted


@pytest.mark.asyncio
async def test_places_search_returns_named_pois(monkeypatch):
    from tools import places
    import httpx
    client = _FakeAsyncClient()
    client.responses["https://overpass-api.de"] = _FakeResponse(200, {
        "elements": [
            {"type": "node", "id": 1, "lat": 59.91, "lon": 10.75,
             "tags": {"name": "Cafe A", "amenity": "cafe",
                      "opening_hours": "Mo-Fr 08:00-18:00"}},
            {"type": "node", "id": 2, "lat": 59.912, "lon": 10.752,
             "tags": {"name": "Cafe B", "amenity": "cafe"}},
        ],
    })
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: client)
    out = await places.places_search.handler({
        "query": "cafe", "lat": 59.91, "lon": 10.75, "radius_m": 500,
    })
    assert len(out["data"]["places"]) == 2
    by_name = {p["name"]: p for p in out["data"]["places"]}
    assert by_name["Cafe A"]["hours"] == "Mo-Fr 08:00-18:00"
    assert by_name["Cafe B"]["hours"] is None
