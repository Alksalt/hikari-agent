"""Phase 10: daily morning weather brief."""
from __future__ import annotations
import importlib
import os
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

@pytest.mark.asyncio
async def test_morning_brief_skips_when_toggled_off(monkeypatch):
    db.upsert_core_block("morning_brief_status", "disabled")
    sent: list[str] = []
    async def fake_send(s): sent.append(s)
    from agents import morning_brief
    fired = await morning_brief.maybe_send_morning_brief(fake_send)
    assert fired is False
    assert sent == []

@pytest.mark.asyncio
async def test_morning_brief_uses_home_when_no_share(monkeypatch):
    monkeypatch.setenv("HOME_LAT", "59.91")
    monkeypatch.setenv("HOME_LON", "10.75")
    captured = {}
    async def fake_fetch(lat, lon):
        captured["lat"] = lat
        captured["lon"] = lon
        return {"sources": {"open_meteo": {"temp_high_c": 18, "temp_low_c": 10}},
                "consensus": {"values": {"temp_high_c": 18, "temp_low_c": 10}, "disagree": []}}
    async def fake_run_proactive(prompt, **kwargs): return "morning. high 18, low 10."
    async def fake_send(s): pass
    from agents import morning_brief
    monkeypatch.setattr(morning_brief, "fetch_forecast", fake_fetch)
    monkeypatch.setattr(morning_brief, "run_proactive", fake_run_proactive)
    await morning_brief.maybe_send_morning_brief(fake_send)
    assert captured["lat"] == 59.91
    assert captured["lon"] == 10.75

@pytest.mark.asyncio
async def test_morning_brief_ignores_stale_location(monkeypatch):
    """Location older than max_stale_location_hours should be ignored."""
    import json
    from datetime import UTC, datetime, timedelta
    stale = (datetime.now(UTC) - timedelta(hours=72)).isoformat()
    state = {"lat": 35.68, "lon": 139.69, "label": "Tokyo",
             "shared_at": stale, "defer_until_counter": 0}
    db.runtime_set("user_location_state", json.dumps(state))
    monkeypatch.setenv("HOME_LAT", "59.91")
    monkeypatch.setenv("HOME_LON", "10.75")
    captured = {}
    async def fake_fetch(lat, lon):
        captured["lat"] = lat
        return {"sources": {"open_meteo": {"temp_high_c": 20, "temp_low_c": 10}},
                "consensus": {"values": {"temp_high_c": 20, "temp_low_c": 10}, "disagree": []}}
    async def fake_run_proactive(prompt, **kwargs): return "morning."
    async def fake_send(s): pass
    from agents import morning_brief
    monkeypatch.setattr(morning_brief, "fetch_forecast", fake_fetch)
    monkeypatch.setattr(morning_brief, "run_proactive", fake_run_proactive)
    await morning_brief.maybe_send_morning_brief(fake_send)
    # Should fall through to HOME, not use Tokyo
    assert captured["lat"] == 59.91


@pytest.mark.asyncio
async def test_morning_brief_prefers_recent_share(monkeypatch):
    import json
    from datetime import UTC, datetime
    state = {"lat": 35.68, "lon": 139.69, "label": "Tokyo",
             "shared_at": datetime.now(UTC).isoformat(), "defer_until_counter": 0}
    db.runtime_set("user_location_state", json.dumps(state))
    monkeypatch.setenv("HOME_LAT", "59.91")
    monkeypatch.setenv("HOME_LON", "10.75")
    captured = {}
    async def fake_fetch(lat, lon):
        captured["lat"] = lat
        return {"sources": {"open_meteo": {"temp_high_c": 25, "temp_low_c": 18}},
                "consensus": {"values": {"temp_high_c": 25, "temp_low_c": 18}, "disagree": []}}
    async def fake_run_proactive(prompt, **kwargs): return "morning."
    async def fake_send(s): pass
    from agents import morning_brief
    monkeypatch.setattr(morning_brief, "fetch_forecast", fake_fetch)
    monkeypatch.setattr(morning_brief, "run_proactive", fake_run_proactive)
    await morning_brief.maybe_send_morning_brief(fake_send)
    assert captured["lat"] == 35.68
