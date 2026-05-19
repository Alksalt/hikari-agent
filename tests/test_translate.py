"""Phase 10: translation tool (DeepL + LibreTranslate fallback + romaji)."""
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
        self.status_code = status_code
        self._json = json_data or {}
    def json(self): return self._json
    def raise_for_status(self):
        if self.status_code >= 400: raise Exception(f"HTTP {self.status_code}")


class _FakeAsyncClient:
    def __init__(self, *a, **k): self.responses = {}; self.posts = []
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        for prefix, resp in self.responses.items():
            if url.startswith(prefix): return resp
        raise Exception(f"no mock for {url}")


@pytest.mark.asyncio
async def test_translate_via_deepl_when_key_set(monkeypatch):
    monkeypatch.setenv("DEEPL_API_KEY", "test-key")
    from tools import translate
    import httpx
    client = _FakeAsyncClient()
    client.responses["https://api-free.deepl.com"] = _FakeResponse(200, {
        "translations": [{"text": "Привет", "detected_source_language": "EN"}],
    })
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: client)
    out = await translate.translate.handler({"text": "Hello", "target": "ru"})
    assert out["data"]["translated_text"] == "Привет"
    assert out["data"]["backend"] == "deepl"


@pytest.mark.asyncio
async def test_translate_falls_back_to_libretranslate_when_no_key(monkeypatch):
    monkeypatch.delenv("DEEPL_API_KEY", raising=False)
    from tools import translate
    import httpx
    client = _FakeAsyncClient()
    client.responses["https://libretranslate.com"] = _FakeResponse(200, {
        "translatedText": "Hei",
    })
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: client)
    out = await translate.translate.handler({"text": "Hello", "target": "no"})
    assert out["data"]["translated_text"] == "Hei"
    assert out["data"]["backend"] == "libretranslate"


@pytest.mark.asyncio
async def test_translate_japanese_with_romaji(monkeypatch):
    monkeypatch.setenv("DEEPL_API_KEY", "test-key")
    from tools import translate
    import httpx
    client = _FakeAsyncClient()
    client.responses["https://api-free.deepl.com"] = _FakeResponse(200, {
        "translations": [{"text": "こんにちは", "detected_source_language": "EN"}],
    })
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: client)
    out = await translate.translate.handler({"text": "Hello", "target": "ja_romaji"})
    assert out["data"]["translated_text"] == "こんにちは"
    assert out["data"]["transliteration"] is not None
    assert "konnichi" in out["data"]["transliteration"].lower()


@pytest.mark.asyncio
async def test_translate_rejects_unsupported_target():
    from tools import translate
    out = await translate.translate.handler({"text": "Hi", "target": "klingon"})
    assert "refused" in out["content"][0]["text"].lower()
