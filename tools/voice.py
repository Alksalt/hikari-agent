"""Voice STT — transcribe inbound Telegram voice notes.

Uses the OpenAI Whisper API exclusively (``openai_whisper_api``). The local
faster-whisper path was removed 2026-05-30 — it did not work reliably.

Configure via ``voice.*`` keys in ``config/engagement.yaml``:

* ``transcription_provider: openai_whisper_api`` — required; any other value
  raises :class:`VoiceTranscribeError` immediately (hard config error, not a
  silent fallback).
* ``transcribe_endpoint`` — OpenAI transcription URL.
* ``transcribe_model`` — e.g. ``"whisper-1"``.
* ``transcribe_api_key_env`` — env-var name holding the OpenAI key
  (``OPENAI_API_KEY``).  Missing key raises loudly when a voice note arrives.

Single async entrypoint :func:`transcribe_voice` handles the full upload /
transcribe cycle. Failure modes raise :class:`VoiceTranscribeError` with a
clear message; callers (e.g. the Telegram bridge's ``handle_voice``) should
send the configured ``voice.graceful_failure_reply`` to the user rather than
crashing the handler.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx

from agents import config as cfg

logger = logging.getLogger(__name__)


class VoiceTranscribeError(Exception):
    """Raised when STT can't produce a transcript for any reason."""


def _enabled() -> bool:
    return bool(cfg.get("voice.enabled", True))


def _endpoint() -> str:
    val = cfg.get("voice.transcribe_endpoint")
    if not val:
        raise VoiceTranscribeError("voice.transcribe_endpoint not configured")
    return str(val)


def _model() -> str:
    val = cfg.get("voice.transcribe_model")
    if not val:
        raise VoiceTranscribeError("voice.transcribe_model not configured")
    return str(val)


def _api_key() -> str:
    env_var = cfg.get("voice.transcribe_api_key_env")
    if not env_var:
        raise VoiceTranscribeError("voice.transcribe_api_key_env not configured")
    key = os.environ.get(str(env_var))
    if not key:
        raise VoiceTranscribeError(
            f"transcription API key env {env_var!r} not set — "
            "set OPENAI_API_KEY to enable voice transcription"
        )
    return key


def _language() -> str | None:
    # null in yaml -> None -> auto-detect.
    val = cfg.get("voice.language")
    if val is None:
        return None
    s = str(val).strip()
    return s or None


def _max_duration_sec() -> float:
    return float(cfg.get("voice.max_duration_sec", 300))


def _request_timeout_sec() -> float:
    # Whisper for short clips is fast; allow a generous ceiling but pull from
    # config if/when a knob is added. For now we don't expose a separate key.
    return float(cfg.get("voice.request_timeout_sec", 60.0))


async def _transcribe_via_openai(path: Path) -> str:
    """Upload ``path`` to the configured OpenAI Whisper endpoint and return text."""
    # API key check first — fail fast before reading bytes.
    api_key = _api_key()
    endpoint = _endpoint()
    model = _model()

    try:
        audio_bytes = path.read_bytes()
    except OSError as e:
        raise VoiceTranscribeError(f"could not read audio file: {e}") from e

    if not audio_bytes:
        raise VoiceTranscribeError("audio file is empty")

    data: dict[str, str] = {"model": model}
    lang = _language()
    if lang:
        data["language"] = lang

    files = {
        "file": (path.name, audio_bytes, "audio/ogg"),
    }
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        async with httpx.AsyncClient(timeout=_request_timeout_sec()) as client:
            resp = await client.post(
                endpoint,
                headers=headers,
                data=data,
                files=files,
            )
    except httpx.HTTPError as e:
        raise VoiceTranscribeError(f"transcription request failed: {e}") from e

    if resp.status_code >= 400:
        body_preview = (resp.text or "")[:200]
        logger.warning(
            "voice STT HTTP %s from %s: %s", resp.status_code, endpoint, body_preview
        )
        raise VoiceTranscribeError(f"transcription HTTP {resp.status_code}")

    try:
        payload = resp.json()
    except ValueError as e:
        raise VoiceTranscribeError(f"transcription response not JSON: {e}") from e

    transcript = (payload or {}).get("text")
    if not transcript or not str(transcript).strip():
        raise VoiceTranscribeError("transcription response missing 'text'")

    return str(transcript).strip()


async def transcribe_voice(audio_path: Path) -> str:
    """Transcribe ``audio_path`` via the OpenAI Whisper API and return text.

    Reads ``voice.transcription_provider`` from config and requires it to be
    ``openai_whisper_api``; any other value raises :class:`VoiceTranscribeError`
    immediately (hard config error — the local faster-whisper path was removed).

    Raises :class:`VoiceTranscribeError` on disabled-config, bad provider,
    missing API key, missing file, HTTP error, or empty response.  The bridge
    handler should catch this and surface ``voice.graceful_failure_reply`` to
    the user.
    """
    if not _enabled():
        raise VoiceTranscribeError("voice transcription is disabled in config")

    path = Path(audio_path)
    if not path.is_file():
        raise VoiceTranscribeError(f"audio file not found: {path}")

    provider = str(cfg.get("voice.transcription_provider", "openai_whisper_api"))
    if provider != "openai_whisper_api":
        raise VoiceTranscribeError(
            f"unsupported voice.transcription_provider {provider!r} — "
            "only 'openai_whisper_api' is supported (local faster-whisper was removed)"
        )

    return await _transcribe_via_openai(path)
