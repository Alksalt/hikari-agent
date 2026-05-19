"""Stage A voice STT tests — pure-function tests on ``tools.voice.transcribe_voice``.

The Telegram bridge handler (``handle_voice``) is not exercised here: the
python-telegram-bot mocks are heavy and the handler is a thin wiring layer on
top of the function we DO test. Coverage focuses on the config-gated paths
and the HTTP error surface, matching the multimodal test pattern.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from agents import config
from storage import db
from tools import voice as voice_tool


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    config.reload()
    yield


def _write_voice_yaml(
    tmp_path: Path,
    monkeypatch,
    *,
    enabled: bool = True,
    api_key_env: str = "OPENAI_API_KEY",
    language: str | None = None,
) -> Path:
    lang_yaml = f'"{language}"' if language else "null"
    cfg_text = (
        "voice:\n"
        f"  enabled: {'true' if enabled else 'false'}\n"
        "  transcription_provider: openai_whisper_api\n"
        "  max_duration_sec: 300\n"
        f"  language: {lang_yaml}\n"
        '  transcribe_endpoint: "https://api.openai.com/v1/audio/transcriptions"\n'
        '  transcribe_model: "whisper-1"\n'
        f"  transcribe_api_key_env: {api_key_env}\n"
        '  save_dir: "data/user_voice"\n'
        '  transcript_prefix: "[voice note]"\n'
        '  graceful_failure_reply: "(can\'t transcribe right now.)"\n'
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()
    return p


# ---------- HTTP mocks (mirror tests/test_multimodal.py:_FakeAsyncClient) ----------

class _FakeResponse:
    def __init__(self, status_code: int, json_data: dict | None = None,
                 text: str = ""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text or ""

    def json(self):
        if not isinstance(self._json, dict) and not isinstance(self._json, list):
            raise ValueError("not JSON")
        return self._json


class _FakeAsyncClient:
    """Default fake: returns a successful Whisper-shaped JSON body."""

    LAST_DATA: dict = {}
    LAST_FILES_KEYS: list = []
    LAST_HEADERS: dict = {}

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def post(self, url, headers=None, data=None, files=None):
        _FakeAsyncClient.LAST_DATA = dict(data or {})
        _FakeAsyncClient.LAST_FILES_KEYS = list((files or {}).keys())
        _FakeAsyncClient.LAST_HEADERS = dict(headers or {})
        return _FakeResponse(200, {"text": "hello from whisper"})


class _FakeAsyncClient400:
    def __init__(self, *a, **kw):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return None
    async def post(self, url, headers=None, data=None, files=None):
        return _FakeResponse(400, text='{"error": "bad request"}')


class _FakeAsyncClient500:
    def __init__(self, *a, **kw):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return None
    async def post(self, url, headers=None, data=None, files=None):
        return _FakeResponse(500, text="upstream boom")


class _FakeAsyncClientEmptyText:
    def __init__(self, *a, **kw):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return None
    async def post(self, url, headers=None, data=None, files=None):
        return _FakeResponse(200, {"text": "   "})


# ---------- tests ----------

def _make_audio(tmp_path: Path) -> Path:
    p = tmp_path / "note.ogg"
    p.write_bytes(b"OggS\x00fake-audio-bytes")
    return p


async def test_raises_when_disabled(tmp_path, monkeypatch):
    _write_voice_yaml(tmp_path, monkeypatch, enabled=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    audio = _make_audio(tmp_path)
    with pytest.raises(voice_tool.VoiceTranscribeError) as exc:
        await voice_tool.transcribe_voice(audio)
    assert "disabled" in str(exc.value).lower()


async def test_raises_when_api_key_missing(tmp_path, monkeypatch):
    _write_voice_yaml(tmp_path, monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    audio = _make_audio(tmp_path)
    with pytest.raises(voice_tool.VoiceTranscribeError) as exc:
        await voice_tool.transcribe_voice(audio)
    msg = str(exc.value).lower()
    assert "api key" in msg or "openai_api_key" in msg


async def test_returns_transcript_on_success(tmp_path, monkeypatch):
    _write_voice_yaml(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    audio = _make_audio(tmp_path)
    text = await voice_tool.transcribe_voice(audio)
    assert text == "hello from whisper"
    # The model knob came from config and made it into the multipart body.
    assert _FakeAsyncClient.LAST_DATA.get("model") == "whisper-1"
    # No language key when config language is null.
    assert "language" not in _FakeAsyncClient.LAST_DATA
    # The Authorization header is present (we don't assert the key value to
    # avoid coupling to env scrubbing).
    assert "Authorization" in _FakeAsyncClient.LAST_HEADERS
    assert _FakeAsyncClient.LAST_FILES_KEYS == ["file"]


async def test_includes_language_when_configured(tmp_path, monkeypatch):
    _write_voice_yaml(tmp_path, monkeypatch, language="en")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    audio = _make_audio(tmp_path)
    text = await voice_tool.transcribe_voice(audio)
    assert text == "hello from whisper"
    assert _FakeAsyncClient.LAST_DATA.get("language") == "en"


async def test_raises_on_http_400(tmp_path, monkeypatch):
    _write_voice_yaml(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient400)
    audio = _make_audio(tmp_path)
    with pytest.raises(voice_tool.VoiceTranscribeError) as exc:
        await voice_tool.transcribe_voice(audio)
    assert "400" in str(exc.value)


async def test_raises_on_http_500(tmp_path, monkeypatch):
    _write_voice_yaml(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient500)
    audio = _make_audio(tmp_path)
    with pytest.raises(voice_tool.VoiceTranscribeError) as exc:
        await voice_tool.transcribe_voice(audio)
    assert "500" in str(exc.value)


async def test_raises_on_empty_transcript(tmp_path, monkeypatch):
    _write_voice_yaml(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClientEmptyText)
    audio = _make_audio(tmp_path)
    with pytest.raises(voice_tool.VoiceTranscribeError):
        await voice_tool.transcribe_voice(audio)


async def test_raises_when_file_missing(tmp_path, monkeypatch):
    _write_voice_yaml(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with pytest.raises(voice_tool.VoiceTranscribeError) as exc:
        await voice_tool.transcribe_voice(tmp_path / "missing.ogg")
    assert "not found" in str(exc.value).lower()
