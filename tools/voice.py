"""Voice STT — transcribe inbound Telegram voice notes.

Supports two providers via ``voice.transcription_provider`` in
``config/engagement.yaml``:

* ``openai_whisper_api`` (default) — uploads to OpenAI Whisper REST endpoint.
* ``local_faster_whisper`` — runs ``faster-whisper`` locally; no API key needed.

Single async entrypoint :func:`transcribe_voice` dispatches to the correct
provider. Failure modes raise :class:`VoiceTranscribeError` with a clean
message; callers (e.g. the Telegram bridge's ``handle_voice``) should send the
configured ``voice.graceful_failure_reply`` to the user rather than crashing
the handler.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from agents import config as cfg

if TYPE_CHECKING:
    from faster_whisper import WhisperModel  # noqa: F401 — type-check only

logger = logging.getLogger(__name__)

# Lazy-loaded faster-whisper model — initialised once, reused across calls.
# Init guarded by _FW_INIT_LOCK so two concurrent voice notes running inside
# run_in_executor cannot race the singleton construction.
_FW_MODEL: "WhisperModel | None" = None
_FW_INIT_LOCK: asyncio.Lock | None = None


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
            f"transcription API key env {env_var!r} not set"
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


async def _ensure_fw_model_loaded() -> None:
    """Lazy-load the faster-whisper singleton, race-safe.

    The init lock serialises construction across concurrent voice notes;
    once loaded, subsequent calls bypass the lock with a None-check fast path.
    Held only during construction — transcription itself remains parallel.
    """
    global _FW_MODEL, _FW_INIT_LOCK
    if _FW_MODEL is not None:
        return
    if _FW_INIT_LOCK is None:
        _FW_INIT_LOCK = asyncio.Lock()
    async with _FW_INIT_LOCK:
        if _FW_MODEL is not None:  # another waiter loaded it while we waited
            return
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise VoiceTranscribeError(
                "faster-whisper is not installed; add it via `uv add faster-whisper`"
            ) from e
        model_name = str(cfg.get("voice.local_faster_whisper_model", "base.en"))
        compute_type = str(cfg.get("voice.local_faster_whisper_compute_type", "int8"))
        logger.info("loading faster-whisper model %r (%s)", model_name, compute_type)
        loop = asyncio.get_running_loop()
        try:
            _FW_MODEL = await loop.run_in_executor(
                None,
                lambda: WhisperModel(model_name, device="cpu", compute_type=compute_type),
            )
        except Exception as e:
            raise VoiceTranscribeError(f"faster-whisper model load failed: {e}") from e


def _transcribe_via_faster_whisper(audio_path: Path) -> str:
    """Run faster-whisper locally and return the transcript text.

    Pure-sync function — meant to be called from ``run_in_executor`` so the
    asyncio event loop stays responsive during inference. The singleton
    model must already be loaded by ``_ensure_fw_model_loaded`` before
    calling this; we don't try to construct it here because that path
    needs the async lock to be race-safe.

    Raises :class:`VoiceTranscribeError` if the model isn't loaded or
    transcription fails.
    """
    if _FW_MODEL is None:
        raise VoiceTranscribeError(
            "faster-whisper model not initialised; call _ensure_fw_model_loaded first"
        )
    try:
        segments, _info = _FW_MODEL.transcribe(str(audio_path), beam_size=1)
        text = " ".join(s.text.strip() for s in segments).strip()
    except Exception as e:
        raise VoiceTranscribeError(f"faster-whisper transcription failed: {e}") from e

    if not text:
        raise VoiceTranscribeError("faster-whisper returned empty transcript")
    return text


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
    """Transcribe ``audio_path`` using the configured provider and return text.

    Dispatches to ``local_faster_whisper`` or ``openai_whisper_api`` (default)
    based on ``voice.transcription_provider`` in ``config/engagement.yaml``.
    Unknown providers fall back to ``openai_whisper_api``.

    Raises :class:`VoiceTranscribeError` on disabled-config, missing API key,
    missing file, HTTP error, or empty response. The bridge handler should
    catch this and surface ``voice.graceful_failure_reply`` to the user.
    """
    if not _enabled():
        raise VoiceTranscribeError("voice transcription is disabled in config")

    path = Path(audio_path)
    if not path.is_file():
        raise VoiceTranscribeError(f"audio file not found: {path}")

    provider = str(cfg.get("voice.transcription_provider", "openai_whisper_api"))

    if provider == "local_faster_whisper":
        # Run the sync CTranslate2 inference in a thread so the asyncio event
        # loop (Telegram bridge, scheduler, gatekeeper, etc.) is not blocked
        # for the model-load + decode duration.
        await _ensure_fw_model_loaded()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, _transcribe_via_faster_whisper, path,
        )

    # openai_whisper_api is the default; unknown values also fall through here.
    if provider != "openai_whisper_api":
        logger.warning(
            "unknown voice.transcription_provider %r — falling back to openai_whisper_api",
            provider,
        )
    return await _transcribe_via_openai(path)
