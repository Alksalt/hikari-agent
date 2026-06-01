"""Provider dispatch tests for tools.voice.transcribe_voice.

The local faster-whisper path was removed 2026-05-30.  These tests cover:
  - openai_whisper_api provider routes to the OpenAI HTTP path (happy path)
  - unknown / unsupported provider raises VoiceTranscribeError immediately
    (hard config error, not a silent fallback)
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from agents import config

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_audio(tmp_path: Path) -> Path:
    p = tmp_path / "note.ogg"
    p.write_bytes(b"OggS\x00fake-audio-bytes")
    return p


def _voice_yaml(tmp_path: Path, monkeypatch, *, provider: str) -> None:
    cfg_text = (
        "voice:\n"
        "  enabled: true\n"
        f"  transcription_provider: {provider}\n"
        "  max_duration_sec: 300\n"
        "  language: null\n"
        '  transcribe_endpoint: "https://api.openai.com/v1/audio/transcriptions"\n'
        '  transcribe_model: "whisper-1"\n'
        "  transcribe_api_key_env: OPENAI_API_KEY\n"
        '  save_dir: "data/user_voice"\n'
        '  transcript_prefix: "[voice note]"\n'
        '  graceful_failure_reply: "(can\'t transcribe right now.)"\n'
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    config.reload()
    yield


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_provider_dispatch_to_openai_when_configured(tmp_path, monkeypatch):
    """openai_whisper_api provider routes to the OpenAI HTTP path."""
    _voice_yaml(tmp_path, monkeypatch, provider="openai_whisper_api")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    audio = _make_audio(tmp_path)

    import tools.voice as vmod

    openai_called = []

    async def fake_openai(path):
        openai_called.append(str(path))
        return "openai transcript"

    monkeypatch.setattr(vmod, "_transcribe_via_openai", fake_openai)

    result = await vmod.transcribe_voice(audio)
    assert result == "openai transcript"
    assert openai_called, "OpenAI path was not called"


async def test_unknown_provider_raises_loudly(tmp_path, monkeypatch):
    """An unrecognised provider value raises VoiceTranscribeError immediately."""
    _voice_yaml(tmp_path, monkeypatch, provider="garbage_provider")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    audio = _make_audio(tmp_path)

    import tools.voice as vmod

    with pytest.raises(vmod.VoiceTranscribeError) as exc:
        await vmod.transcribe_voice(audio)

    msg = str(exc.value).lower()
    assert "garbage_provider" in msg or "unsupported" in msg, (
        f"Expected provider name or 'unsupported' in error message, got: {exc.value!r}"
    )


async def test_local_faster_whisper_provider_raises_loudly(tmp_path, monkeypatch):
    """local_faster_whisper raises VoiceTranscribeError — path was removed."""
    _voice_yaml(tmp_path, monkeypatch, provider="local_faster_whisper")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    audio = _make_audio(tmp_path)

    import tools.voice as vmod

    with pytest.raises(vmod.VoiceTranscribeError) as exc:
        await vmod.transcribe_voice(audio)

    msg = str(exc.value).lower()
    assert "local_faster_whisper" in msg or "unsupported" in msg or "removed" in msg, (
        f"Expected informative error about removed provider, got: {exc.value!r}"
    )
