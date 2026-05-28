"""Tests for tools/voice_outbound.py — Phase E."""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agents import config as cfg
from storage import db


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _result_text(result: dict) -> str:
    return result["content"][0]["text"]


def _handler(vob):
    """Return the underlying async callable, bypassing the SDK @tool wrapper."""
    h = getattr(vob.voice_outbound_send, "handler", vob.voice_outbound_send)
    if not callable(h):
        h = vob.voice_outbound_send
    return h


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Each test gets a fresh on-disk DB and an isolated VOICE_OUTBOX."""
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    cfg.reload()

    import tools.voice_outbound as vob
    outbox = tmp_path / "voice_outbox"
    monkeypatch.setattr(vob, "VOICE_OUTBOX", outbox)
    yield outbox


@pytest.fixture()
def _good_env(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key-123")


@pytest.fixture()
def _voice_id_set(monkeypatch):
    import tools.voice_outbound as vob
    monkeypatch.setattr(vob, "_voice_id", lambda: "test-voice-id")


@pytest.fixture()
def _mood_focused(monkeypatch):
    import tools.voice_outbound as vob
    monkeypatch.setattr(vob, "_resolve_mood", lambda: ("focused", "default"))


def _mock_tts(monkeypatch, mp3_bytes=b"fakemp3"):
    import tools.voice_outbound as vob
    async def fake_tts(text, voice_id, profile):
        return mp3_bytes
    monkeypatch.setattr(vob, "_tts_mp3", fake_tts)


def _mock_ogg(monkeypatch, duration=3.5):
    import tools.voice_outbound as vob
    async def fake_ogg(mp3_bytes, out_path):
        out_path.write_bytes(b"fakeogg")
        return duration
    monkeypatch.setattr(vob, "_mp3_to_ogg", fake_ogg)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_writes_to_media_outbox(_good_env, _voice_id_set, _mood_focused, monkeypatch):
    import tools.voice_outbound as vob
    _mock_tts(monkeypatch)
    _mock_ogg(monkeypatch)
    result = await _handler(vob)({"text": "hey. sleep.", "force": False})
    text = _result_text(result)
    assert text.startswith("queued voice_")
    assert "3.5s" in text

    rows = db.media_outbox_pending(kind="voice")
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert payload["text_transcript"] == "hey. sleep."
    assert payload["provider"] == "elevenlabs_flash_v2_5"


@pytest.mark.asyncio
async def test_respects_daily_cap(_good_env, _voice_id_set, _mood_focused, monkeypatch):
    import tools.voice_outbound as vob
    monkeypatch.setattr(vob, "_cap", lambda: 2)
    monkeypatch.setattr(vob, "_sent_today", lambda: 2)
    result = await _handler(vob)({"text": "hello"})
    assert _result_text(result) == "refused: daily_cap"


@pytest.mark.asyncio
async def test_mood_gated_low_tolerance(_good_env, _voice_id_set, monkeypatch):
    import tools.voice_outbound as vob
    monkeypatch.setattr(vob, "_resolve_mood", lambda: ("focused", "low-tolerance-late"))
    result = await _handler(vob)({"text": "hi", "force": False})
    assert _result_text(result) == "refused: cycle_low_tolerance"


@pytest.mark.asyncio
async def test_mood_gated_irritable(_good_env, _voice_id_set, monkeypatch):
    import tools.voice_outbound as vob
    monkeypatch.setattr(vob, "_resolve_mood", lambda: ("irritable", "default"))
    result = await _handler(vob)({"text": "fine"})
    assert _result_text(result) == "refused: mood_irritable"


@pytest.mark.asyncio
async def test_force_bypasses_mood(_good_env, _voice_id_set, monkeypatch):
    import tools.voice_outbound as vob
    monkeypatch.setattr(vob, "_resolve_mood", lambda: ("irritable", "low-tolerance"))
    _mock_tts(monkeypatch)
    _mock_ogg(monkeypatch)
    result = await _handler(vob)({"text": "forced note", "force": True})
    assert _result_text(result).startswith("queued voice_")


@pytest.mark.asyncio
async def test_not_configured(_voice_id_set, _mood_focused, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    import tools.voice_outbound as vob
    result = await _handler(vob)({"text": "hey"})
    assert _result_text(result) == "refused: not_configured"


@pytest.mark.asyncio
async def test_ffmpeg_missing(_good_env, _voice_id_set, _mood_focused, monkeypatch):
    import tools.voice_outbound as vob
    _mock_tts(monkeypatch)

    async def fake_ogg_missing(mp3_bytes, out_path):
        raise RuntimeError("ffmpeg_missing")

    monkeypatch.setattr(vob, "_mp3_to_ogg", fake_ogg_missing)
    result = await _handler(vob)({"text": "test"})
    assert _result_text(result) == "refused: ffmpeg_missing"
    # No orphan ogg files in the outbox
    outbox = vob.VOICE_OUTBOX
    ogg_files = list(outbox.glob("*.ogg")) if outbox.exists() else []
    assert ogg_files == []


def test_outbox_dispatcher_has_voice_kind():
    """Regression guard — Phase A blast-radius finding."""
    from agents.telegram_bridge import _OUTBOX_DISPATCHERS
    assert "voice" in _OUTBOX_DISPATCHERS


@pytest.mark.asyncio
async def test_send_outbox_voice_out_of_tree_aborts(monkeypatch, tmp_path):
    from agents.telegram_bridge import _send_outbox_voice

    # Create a real ogg file outside the voice outbox
    outside = tmp_path / "outside.ogg"
    outside.write_bytes(b"data")

    row = {
        "id": 999,
        "payload_json": json.dumps({"path": str(outside), "duration_sec": 1.0}),
    }

    import tools.voice_outbound as vob
    # VOICE_OUTBOX is a different subdir — outside is not inside it
    voice_outbox = tmp_path / "voice_outbox"
    voice_outbox.mkdir()
    monkeypatch.setattr(vob, "VOICE_OUTBOX", voice_outbox)

    with patch("storage.db.media_outbox_mark_aborted") as mock_abort:
        result = await _send_outbox_voice(None, 123, row)
    assert result is None
    mock_abort.assert_called_once_with(999, "out_of_tree")


@pytest.mark.asyncio
async def test_voice_id_unset_refuses(_good_env, _mood_focused, monkeypatch):
    import tools.voice_outbound as vob
    monkeypatch.setattr(vob, "_voice_id", lambda: None)
    result = await _handler(vob)({"text": "hello"})
    assert _result_text(result) == "refused: voice_id_unset"
