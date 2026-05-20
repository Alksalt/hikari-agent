"""Phase 10: YouTube Music tools."""
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


class _FakeYTMusic:
    def __init__(self, *a, **k): pass
    def get_history(self):
        return [
            {"title": "Track A", "artists": [{"name": "Artist A"}], "videoId": "vidA",
             "played": "Today"},
            {"title": "Track B", "artists": [{"name": "Artist B"}], "videoId": "vidB",
             "played": "Yesterday"},
        ]
    def search(self, q, filter=None, limit=20):
        return [{"title": f"hit for {q}", "artists": [{"name": "X"}], "videoId": "v"}]
    def get_library_songs(self, limit=25):
        return [{"title": "liked T", "artists": [{"name": "L"}], "videoId": "vL"}]


@pytest.mark.asyncio
async def test_ytmusic_recent_returns_history(monkeypatch):
    monkeypatch.setenv("YTMUSIC_BROWSER_JSON_PATH", "/dev/null")
    from tools import ytmusic
    from tools.ytmusic import _shared
    monkeypatch.setattr(_shared, "_client", lambda: _FakeYTMusic())
    out = await ytmusic.ytmusic_recent.handler({"limit": 5})
    tracks = out["data"]["tracks"]
    assert len(tracks) == 2
    assert tracks[0]["title"] == "Track A"


@pytest.mark.asyncio
async def test_ytmusic_search_calls_through(monkeypatch):
    monkeypatch.setenv("YTMUSIC_BROWSER_JSON_PATH", "/dev/null")
    from tools import ytmusic
    from tools.ytmusic import _shared
    monkeypatch.setattr(_shared, "_client", lambda: _FakeYTMusic())
    out = await ytmusic.ytmusic_search.handler({"query": "lofi", "filter": "songs"})
    assert len(out["data"]["results"]) >= 1
    assert "lofi" in out["data"]["results"][0]["title"]


@pytest.mark.asyncio
async def test_ytmusic_returns_graceful_msg_when_unauthed(monkeypatch):
    monkeypatch.delenv("YTMUSIC_BROWSER_JSON_PATH", raising=False)
    from tools import ytmusic
    from tools.ytmusic import _shared
    def _raise(): raise FileNotFoundError("no cookie blob")
    monkeypatch.setattr(_shared, "_client", _raise)
    out = await ytmusic.ytmusic_recent.handler({"limit": 5})
    text = out["content"][0]["text"].lower()
    assert "yt music" in text or "auth" in text or "configured" in text
