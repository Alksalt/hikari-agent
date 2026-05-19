"""Stage-4 multimodal tests: reactions probability/cooldown, location flow."""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from agents import config, reactions
from storage import db
from tools import location as location_tool


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    config.reload()
    yield


# ---------- reactions ----------

def test_reactions_disabled_returns_false(monkeypatch, tmp_path):
    cfg_text = "reactions:\n  enabled: false\n  pool: ['👀']\n"
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()
    assert not reactions.should_react(now_counter=100)


def test_reactions_cooldown_blocks(monkeypatch, tmp_path):
    cfg_text = (
        "reactions:\n"
        "  enabled: true\n"
        "  probability_per_inbound: 1.0\n"   # force-on
        "  cooldown_min_messages: 5\n"
        "  pool: ['👀']\n"
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()

    # First call: should react
    assert reactions.should_react(now_counter=1)
    # Record reaction at counter=1.
    db.runtime_set("reactions_last_at_counter", 1)
    # Cooldown blocks counters 2-5.
    assert not reactions.should_react(now_counter=3)
    assert not reactions.should_react(now_counter=5)
    # Counter=6 is past the cooldown window.
    assert reactions.should_react(now_counter=6)


def test_reactions_pick_emoji_from_pool(monkeypatch, tmp_path):
    cfg_text = "reactions:\n  enabled: true\n  pool: ['🌙', '🤔']\n"
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()
    for _ in range(20):
        emoji = reactions.pick_emoji()
        assert emoji in ("🌙", "🤔")


def test_reactions_empty_pool_returns_none(monkeypatch, tmp_path):
    cfg_text = "reactions:\n  enabled: true\n  pool: []\n"
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()
    assert reactions.pick_emoji() is None
    assert not reactions.should_react(now_counter=100)


# ---------- location ----------

class _FakeResponse:
    def __init__(self, status_code: int, json_data: dict):
        self.status_code = status_code
        self._json = json_data
    def json(self):
        return self._json


class _FakeAsyncClient:
    def __init__(self, *a, **kw):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return None
    async def get(self, url, params=None, headers=None):
        if "nominatim" in url or "reverse" in url:
            return _FakeResponse(200, {
                "display_name": "Kristiansund, Møre og Romsdal, Norge",
                "address": {"city": "Kristiansund", "country": "Norge"},
            })
        if "open-meteo" in url or "forecast" in url:
            return _FakeResponse(200, {
                "current": {"temperature_2m": 5.0, "wind_speed_10m": 12.0,
                            "weather_code": 61},
            })
        return _FakeResponse(404, {})


@pytest.mark.asyncio
async def test_location_record_writes_state(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    state = await location_tool.record_share(63.11, 7.73)
    assert state["label"] == "Kristiansund"
    assert "5°C" in (state.get("weather") or "")
    # Stored in runtime_state.
    raw = db.runtime_get("user_location_state")
    assert raw
    stored = json.loads(raw)
    assert stored["lat"] == 63.11


def test_location_current_deferred_on_first_turn():
    """Right after a share, current_location should withhold (defer turns)."""
    # Plant a fresh share with defer_until > current counter.
    state = {
        "lat": 1.0, "lon": 2.0, "label": "X", "weather": None,
        "shared_at": datetime.now(UTC).isoformat(),
        "defer_until_counter": 5,
    }
    db.runtime_set("user_location_state", json.dumps(state))
    db.runtime_set("inbound_message_counter", 2)
    assert location_tool.current_location() is None


def test_location_current_surfaces_after_defer():
    state = {
        "lat": 1.0, "lon": 2.0, "label": "X", "weather": None,
        "shared_at": datetime.now(UTC).isoformat(),
        "defer_until_counter": 2,
    }
    db.runtime_set("user_location_state", json.dumps(state))
    db.runtime_set("inbound_message_counter", 5)
    cur = location_tool.current_location()
    assert cur is not None
    assert cur["label"] == "X"


def test_location_stale_is_cleared(monkeypatch, tmp_path):
    cfg_text = (
        "location:\n"
        "  enabled: true\n"
        "  freshness_hours: 1\n"
        "  defer_callback_turns: 0\n"
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()
    state = {
        "lat": 1.0, "lon": 2.0, "label": "X", "weather": None,
        "shared_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
        "defer_until_counter": 0,
    }
    db.runtime_set("user_location_state", json.dumps(state))
    assert location_tool.current_location() is None
    # And the stale entry was cleared.
    assert db.runtime_get("user_location_state") is None


def test_location_format_for_injection_includes_label_and_weather():
    state = {
        "lat": 1.0, "lon": 2.0, "label": "Kristiansund", "weather": "5°C, wind 12",
        "shared_at": datetime.now(UTC).isoformat(),
        "defer_until_counter": 0,
    }
    db.runtime_set("user_location_state", json.dumps(state))
    out = location_tool.format_for_injection()
    assert "Kristiansund" in out
    assert "5°C" in out


def test_location_format_empty_when_disabled(monkeypatch, tmp_path):
    cfg_text = "location:\n  enabled: false\n"
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()
    assert location_tool.format_for_injection() == ""
