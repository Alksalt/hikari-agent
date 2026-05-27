"""Phase D — provider dispatch tests for tools.voice.transcribe_voice.

All four cases mock faster_whisper at the sys.modules level so the package
does not need to be installed for the test suite to pass.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

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
        f"  enabled: true\n"
        f"  transcription_provider: {provider}\n"
        "  local_faster_whisper_model: base.en\n"
        "  local_faster_whisper_compute_type: int8\n"
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


def _install_fw_mock() -> tuple[MagicMock, ModuleType]:
    """Inject a fake faster_whisper into sys.modules; return (WhisperModel mock, module)."""
    fw_mod = ModuleType("faster_whisper")
    model_cls = MagicMock(name="WhisperModel")
    # transcribe() returns (segments_iterable, info); each segment has .text
    fake_segment = MagicMock()
    fake_segment.text = "hello from local"
    model_instance = MagicMock()
    model_instance.transcribe.return_value = ([fake_segment], MagicMock())
    model_cls.return_value = model_instance
    fw_mod.WhisperModel = model_cls
    sys.modules["faster_whisper"] = fw_mod
    return model_cls, fw_mod


def _remove_fw_mock() -> None:
    sys.modules.pop("faster_whisper", None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_fw_model(monkeypatch):
    """Reset the module-level _FW_MODEL cache before each test."""
    import tools.voice as vmod
    monkeypatch.setattr(vmod, "_FW_MODEL", None)
    yield
    monkeypatch.setattr(vmod, "_FW_MODEL", None)


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

async def test_provider_dispatch_to_openai_when_default(tmp_path, monkeypatch):
    """Default / openai_whisper_api provider routes to the OpenAI HTTP path."""
    _voice_yaml(tmp_path, monkeypatch, provider="openai_whisper_api")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    audio = _make_audio(tmp_path)

    import httpx
    import tools.voice as vmod

    openai_called = []

    async def fake_openai(path):
        openai_called.append(str(path))
        return "openai transcript"

    monkeypatch.setattr(vmod, "_transcribe_via_openai", fake_openai)

    result = await vmod.transcribe_voice(audio)
    assert result == "openai transcript"
    assert openai_called, "OpenAI path was not called"


async def test_provider_dispatch_to_local_when_configured(tmp_path, monkeypatch):
    """local_faster_whisper provider routes to faster-whisper, not OpenAI."""
    _voice_yaml(tmp_path, monkeypatch, provider="local_faster_whisper")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    audio = _make_audio(tmp_path)

    model_cls, _fw_mod = _install_fw_mock()
    try:
        import tools.voice as vmod
        importlib.reload(vmod)  # pick up fresh _FW_MODEL = None after reload
        monkeypatch.setattr(vmod, "_FW_MODEL", None)

        openai_called = []

        async def fake_openai(path):  # pragma: no cover
            openai_called.append(str(path))
            return "openai transcript"

        monkeypatch.setattr(vmod, "_transcribe_via_openai", fake_openai)

        result = await vmod.transcribe_voice(audio)
        assert result == "hello from local"
        assert not openai_called, "OpenAI path must NOT be called when provider=local_faster_whisper"
        model_cls.assert_called_once()
    finally:
        _remove_fw_mock()


async def test_local_provider_caches_model(tmp_path, monkeypatch):
    """Second call with local provider reuses the cached WhisperModel instance."""
    _voice_yaml(tmp_path, monkeypatch, provider="local_faster_whisper")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    audio = _make_audio(tmp_path)

    model_cls, _fw_mod = _install_fw_mock()
    try:
        import tools.voice as vmod
        importlib.reload(vmod)
        monkeypatch.setattr(vmod, "_FW_MODEL", None)

        await vmod.transcribe_voice(audio)
        await vmod.transcribe_voice(audio)

        # WhisperModel() constructor must be called exactly once across both calls.
        assert model_cls.call_count == 1, (
            f"WhisperModel instantiated {model_cls.call_count} times — expected 1 (cached)"
        )
    finally:
        _remove_fw_mock()


async def test_unknown_provider_falls_back_to_openai(tmp_path, monkeypatch):
    """An unrecognised provider value falls back to the OpenAI path."""
    _voice_yaml(tmp_path, monkeypatch, provider="garbage_provider")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    audio = _make_audio(tmp_path)

    import tools.voice as vmod

    openai_called = []

    async def fake_openai(path):
        openai_called.append(str(path))
        return "openai fallback transcript"

    monkeypatch.setattr(vmod, "_transcribe_via_openai", fake_openai)

    result = await vmod.transcribe_voice(audio)
    assert result == "openai fallback transcript"
    assert openai_called, "OpenAI fallback was not triggered for unknown provider"
