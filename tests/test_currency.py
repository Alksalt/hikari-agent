"""Phase 10: currency conversion via frankfurter.app."""
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
    async def get(self, url, **kwargs):
        for prefix, resp in self.responses.items():
            if url.startswith(prefix): return resp
        raise Exception(f"no mock for {url}")


@pytest.mark.asyncio
async def test_currency_convert(monkeypatch):
    from tools import currency
    import httpx
    client = _FakeAsyncClient()
    client.responses["https://api.frankfurter.app"] = _FakeResponse(200, {
        "amount": 100, "base": "USD", "date": "2026-05-19",
        "rates": {"NOK": 1050.5},
    })
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: client)
    out = await currency.currency_convert.handler({"amount": 100, "from_ccy": "USD", "to_ccy": "NOK"})
    assert out["data"]["converted"] == 1050.5


@pytest.mark.asyncio
async def test_currency_convert_rejects_bad_currency():
    from tools import currency
    out = await currency.currency_convert.handler({"amount": 100, "from_ccy": "XXX", "to_ccy": "NOK"})
    assert "error" in out["data"] or "refused" in out["content"][0]["text"].lower() \
        or out["data"].get("converted") is None
