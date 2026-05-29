"""voice_outbound — ElevenLabs Flash v2.5 → OGG/Opus → media_outbox kind='voice'.

Mood-gated, daily-capped, agent-decided. Mirrors tools/photos/generate.py shape.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from claude_agent_sdk import tool

from agents import config as cfg
from storage import db
from tools._annotations import annotations_for

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
VOICE_OUTBOX = Path(
    os.environ.get("HIKARI_VOICE_OUTBOX") or REPO_ROOT / "data" / "voice_outbox"
)

_ELEVEN_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
_PROVIDER = "elevenlabs_flash_v2_5"


def _cap() -> int:
    return int(cfg.get("voice_outbound.daily_cap", 10))


def _allowed_moods() -> list[str]:
    return list(cfg.get("voice_outbound.mood_gates", ["tired", "focused", "weirdly good"]))


def _voice_id() -> str | None:
    return cfg.get("voice_outbound.outbound_voice_id")


def _mood_profile(mood_key: str) -> dict[str, float]:
    profiles = cfg.get("voice_outbound.outbound_mood_profiles", {}) or {}
    default = {"stability": 0.5, "similarity_boost": 0.75, "style": 0.0, "use_speaker_boost": True}
    return {**default, **(profiles.get(mood_key) or profiles.get("default") or {})}


def _resolve_mood() -> tuple[str, str]:
    """Returns (daily_mood, cycle_phase). Reads dedicated mood_today core block."""
    import json as _json
    # P1 fix: read the dedicated mood_today core block instead of cycle_state JSON's
    # unreliable mood_today field — cycle_state is not reliably updated by all writers.
    daily = (db.get_core_block("mood_today") or "focused").strip().lower()
    cycle = "default"
    try:
        raw = db.get_core_block("cycle_state") or ""
        data = _json.loads(raw) if raw else {}
        composite = data.get("composite_label", "")
        parts = [p.strip() for p in composite.split("/")]
        if len(parts) >= 1:
            cycle = parts[0] or "default"
    except Exception:
        pass
    return daily, cycle


def _sent_today() -> int:
    today = time.strftime("%Y-%m-%d")
    if db.runtime_get("voice_outbound_sent_date") != today:
        return 0
    return db.runtime_get_int("voice_outbound_sent_today", 0)


def _bump_sent() -> None:
    # P3 fix: atomic increment via SQL UPSERT — eliminates read-modify-write race.
    # Date-rollover guard: if stored date != today, reset counter to 0 first so
    # the subsequent increment starts from 1 (not from yesterday's total).
    today = time.strftime("%Y-%m-%d")
    if db.runtime_get("voice_outbound_sent_date") != today:
        db.runtime_set("voice_outbound_sent_date", today)
        db.runtime_set("voice_outbound_sent_today", 0)
    db.runtime_increment("voice_outbound_sent_today", by=1)


async def _tts_mp3(text: str, voice_id: str, profile: dict) -> bytes:
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("not_configured")
    body = {
        "model_id": "eleven_flash_v2_5",
        "text": text,
        "voice_settings": {
            "stability": profile["stability"],
            "similarity_boost": profile["similarity_boost"],
            "style": profile.get("style", 0.0),
            "use_speaker_boost": profile.get("use_speaker_boost", True),
        },
    }
    url = _ELEVEN_URL.format(voice_id=voice_id)
    timeout = float(cfg.get("voice_outbound.request_timeout_sec", 30.0))
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers={"xi-api-key": api_key, "accept": "audio/mpeg"}, json=body)
    if resp.status_code >= 400:
        raise RuntimeError(f"elevenlabs_http_{resp.status_code}")
    if not resp.content:
        raise RuntimeError("empty_audio")
    return resp.content


async def _mp3_to_ogg(mp3_bytes: bytes, out_path: Path) -> float:
    """Returns duration_sec via ffprobe, raises on ffmpeg failure."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg_missing")
    tmp_mp3 = out_path.with_suffix(".mp3")
    tmp_mp3.write_bytes(mp3_bytes)
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(tmp_mp3),
            "-c:a", "libopus", "-b:a", "24k", "-application", "voip",
            str(out_path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg_failed: {err.decode()[:200]}")
        if not shutil.which("ffprobe"):
            return 0.0
        probe = await asyncio.create_subprocess_exec(
            "ffprobe", "-i", str(out_path),
            "-show_entries", "format=duration", "-v", "quiet", "-of", "csv=p=0",
            stdout=asyncio.subprocess.PIPE,
        )
        out, _ = await probe.communicate()
        try:
            return float(out.decode().strip() or "0")
        except ValueError:
            return 0.0
    finally:
        tmp_mp3.unlink(missing_ok=True)


@tool(
    "voice_outbound_send",
    "Send a voice note (audio) instead of text. Deliberate, mood-gated, daily-capped. "
    "Call ONLY when text wouldn't carry the line — late-night soft, one-line apology, "
    "a callback that needs your voice. Never on every turn. Refuses on low-tolerance / "
    "irritable. Pass force=true ONLY when the user explicitly asked for a voice note. "
    "Returns {ok, idempotency_key, reason}.",
    {"text": str, "force": bool},
    annotations=annotations_for("voice_outbound_send"),
)
async def voice_outbound_send(args: dict[str, Any]) -> dict[str, Any]:
    text = (args.get("text") or "").strip()
    force = bool(args.get("force", False))
    if not text:
        return {"content": [{"type": "text", "text": "refused: empty_text"}]}
    if len(text) > int(cfg.get("voice_outbound.max_chars", 400)):
        return {"content": [{"type": "text", "text": "refused: text_too_long"}]}

    daily_mood, cycle_phase = _resolve_mood()
    if not force:
        if daily_mood == "irritable":
            return {"content": [{"type": "text", "text": "refused: mood_irritable"}]}
        if cycle_phase.startswith("low-tolerance"):
            return {"content": [{"type": "text", "text": "refused: cycle_low_tolerance"}]}
        if daily_mood not in _allowed_moods():
            return {"content": [{"type": "text", "text": f"refused: mood_{daily_mood}"}]}

    if _sent_today() >= _cap():
        return {"content": [{"type": "text", "text": "refused: daily_cap"}]}

    voice_id = _voice_id()
    if not voice_id:
        return {"content": [{"type": "text", "text": "refused: voice_id_unset"}]}

    ikey = f"voice_{uuid.uuid4().hex[:16]}"
    VOICE_OUTBOX.mkdir(parents=True, exist_ok=True)
    ogg_path = VOICE_OUTBOX / f"{ikey}.ogg"

    profile_key = "night" if "night-mode" in cycle_phase else ("tired" if daily_mood == "tired" else "default")
    profile = _mood_profile(profile_key)

    try:
        mp3 = await _tts_mp3(text, voice_id, profile)
    except RuntimeError as e:
        return {"content": [{"type": "text", "text": f"refused: {e}"}]}

    try:
        duration = await _mp3_to_ogg(mp3, ogg_path)
    except RuntimeError as e:
        ogg_path.unlink(missing_ok=True)
        return {"content": [{"type": "text", "text": f"refused: {e}"}]}

    payload = {
        "path": str(ogg_path),
        "duration_sec": duration,
        "text_transcript": text,
        "mood": daily_mood,
        "cycle_phase": cycle_phase,
        "voice_id": voice_id,
        "provider": _PROVIDER,
    }
    row_id = db.media_outbox_insert("voice", ikey, payload)
    if row_id is None:
        ogg_path.unlink(missing_ok=True)
        return {"content": [{"type": "text", "text": "refused: dedup_or_insert_failed"}]}

    _bump_sent()
    # P2 fix: record ElevenLabs cost directly via db.llm_costs_insert.
    # ElevenLabs Flash v2.5 is char-billed at ~$0.50/1M chars.
    # Bypasses runtime._log_aux_cost which lacks a rate entry for this provider.
    try:
        db.llm_costs_insert(
            turn_id=None,
            model="elevenlabs/flash_v2_5",
            path="voice_outbound",
            input_tokens=0,
            output_tokens=0,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=len(text) / 1_000_000 * 0.50,
        )
    except Exception:
        logger.exception("voice_outbound: llm_costs write failed (non-fatal)")

    return {"content": [{"type": "text", "text": f"queued {ikey} ({duration:.1f}s)"}]}


ALL_TOOLS = [voice_outbound_send]
