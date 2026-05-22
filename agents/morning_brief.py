"""Phase 10: daily morning weather brief — proactive at 06:00 local.

Pipeline:
  1. Gate: core_block 'morning_brief_status' must not be 'disabled'
  2. Resolve location: most recent Telegram share (any age) -> HOME_LAT/LON
  3. Fetch multi-source forecast via tools/weather.py
  4. Build prompt, call run_proactive -> Hikari writes in voice
  5. send_text the result
"""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from agents import config as cfg
from agents.runtime import run_visible_proactive
from storage import db
from tools.weather import fetch_forecast

# Phase 13 (Stream C): legacy alias so tests that monkeypatch
# ``morning_brief.run_proactive`` keep working until Stream F updates them.
run_proactive = run_visible_proactive  # noqa: F841

logger = logging.getLogger(__name__)

# Presentation hint injected at the top of the model prompt so Hikari knows
# which render contract to follow when writing the morning brief message.
_WEATHER_PROMPT_HINT = "# presentation_hint: weather_three_window\n\n"


def _is_disabled() -> bool:
    block = (db.get_core_block("morning_brief_status") or "").strip().lower()
    return block in {"disabled", "off", "no"}


def _resolve_location() -> tuple[float, float, str | None] | None:
    """Return (lat, lon, label_or_none) or None if we have nothing."""
    raw = db.runtime_get("user_location_state")
    if raw:
        try:
            state = json.loads(raw)
            max_stale_hours = float(
                cfg.get("morning_brief.max_stale_location_hours", 48)
            )
            shared_at_str = state.get("shared_at")
            if shared_at_str:
                shared_at = datetime.fromisoformat(shared_at_str)
                if shared_at.tzinfo is None:
                    shared_at = shared_at.replace(tzinfo=UTC)
                age_hours = (datetime.now(UTC) - shared_at).total_seconds() / 3600.0
                if age_hours > max_stale_hours:
                    logger.info(
                        "morning_brief: user_location_state is %.1fh old "
                        "(max_stale=%sh) — falling through to HOME env",
                        age_hours, max_stale_hours,
                    )
                    raw = None
            if raw is not None:
                lat = float(state["lat"])
                lon = float(state["lon"])
                return lat, lon, state.get("label")
        except (ValueError, KeyError, TypeError):
            pass
    lat_env = os.environ.get("HOME_LAT")
    lon_env = os.environ.get("HOME_LON")
    if lat_env and lon_env:
        try:
            return float(lat_env), float(lon_env), "home"
        except ValueError:
            pass
    return None


def _build_prompt(forecast: dict[str, Any], label: str | None) -> str:
    from tools.weather._shared import wmo_label  # noqa: PLC0415
    raw_consensus = forecast["consensus"]
    c = raw_consensus["values"]
    disagree = raw_consensus.get("disagree") or []
    sources = ", ".join(forecast["sources"].keys()) or "no sources"
    high = c.get("temp_high_c")
    low = c.get("temp_low_c")
    feels_high = c.get("feels_high_c")
    feels_low = c.get("feels_low_c")
    uv = c.get("uv_index_max")
    wind = c.get("wind_max_kmh")
    rain_max = c.get("precip_prob_max_pct")
    where = label or "where you are"

    windows = forecast.get("windows") or {}
    morning = windows.get("morning") or {}
    midday = windows.get("midday") or {}
    evening = windows.get("evening") or {}
    sunrise = forecast.get("sunrise")
    sunset = forecast.get("sunset")

    disagreement_note = ""
    if disagree:
        disagreement_note = (
            f"\n  note: sources disagree on {', '.join(disagree[:2])} — mention "
            f"briefly only if it fits the voice."
        )

    window_text = ""
    if morning or midday or evening:
        def _win(w: dict, name: str) -> str:
            t = w.get("temp_c")
            cond = wmo_label(w.get("weather_code")) if w.get("weather_code") is not None else ""
            precip = w.get("precip_prob_pct")
            return f"  {name}: {t}°C{' ' + cond if cond else ''}{' rain ' + str(precip) + '%' if precip else ''}"
        window_text = (
            "\n  windows:\n"
            + _win(morning, "morning") + "\n"
            + _win(midday, "midday") + "\n"
            + _win(evening, "evening")
        )

    return (
        _WEATHER_PROMPT_HINT
        + "You are writing a morning weather brief. ONE short message, in your "
        "voice. Hikari is reluctant about being useful — make it dry, never "
        "chirpy. No exclamation marks for enthusiasm. No 'good morning!'. "
        "Stick to short. Don't list bullets.\n\n"
        f"data:\n"
        f"  location: {where}\n"
        f"  high_c: {high}\n"
        f"  low_c: {low}\n"
        f"  feels_high_c: {feels_high}\n"
        f"  feels_low_c: {feels_low}\n"
        f"  precip_prob_max_pct: {rain_max}\n"
        f"  uv_index_max: {uv}\n"
        f"  wind_max_kmh: {wind}\n"
        f"  sunrise: {sunrise}\n"
        f"  sunset: {sunset}\n"
        f"  sources: {sources}"
        f"{window_text}"
        f"{disagreement_note}\n\n"
        "Output ONLY the message text — no preamble, no quotes. If you can't "
        "write something true to her voice, output NO_MESSAGE."
    )


async def maybe_send_morning_brief(send_text) -> bool:
    """Returns True if a brief was sent."""
    from agents import cadence
    from agents.cadence import Pool
    if _is_disabled():
        logger.info("morning_brief: disabled via core_block")
        return False
    if not bool(cfg.get("morning_brief.enabled", True)):
        return False
    allowed, reason = cadence.can_send("morning_brief", Pool.SCHEDULED_CEREMONY)
    if not allowed:
        logger.info("morning_brief: cadence governor vetoed: %s", reason)
        return False
    loc = _resolve_location()
    if loc is None:
        logger.info("morning_brief: no location available (no share, no HOME env)")
        return False
    lat, lon, label = loc
    try:
        forecast = await fetch_forecast(lat, lon)
    except Exception:
        logger.exception("morning_brief: fetch_forecast failed")
        return False
    if not forecast["sources"]:
        logger.info("morning_brief: all weather sources failed")
        return False
    prompt = _build_prompt(forecast, label)
    try:
        # Look up via module globals so tests can monkeypatch
        # ``morning_brief.run_proactive``.
        text = (await run_proactive(prompt)).strip()
    except Exception:
        logger.exception("morning_brief: run_proactive failed")
        return False
    if not text or text.upper().startswith("NO_MESSAGE"):
        return False
    try:
        result = await send_text(text)
    except Exception:
        logger.exception("morning_brief: send_text failed")
        return False
    # Reuse the proactive helper to keep one canonical unpacker (handles
    # both the production 3-tuple and the legacy None-returning fakes).
    from agents.proactive import _unpack_send_result
    final, tg_id, ok = _unpack_send_result(result, text)
    if not ok:
        logger.warning("morning_brief: send_text reported failure; not persisting")
        return False
    # Phase 13.1 (Stream G — codex P0 fix): persist the FINAL filtered text +
    # Telegram message_id post-send.
    try:
        if tg_id is not None:
            db.append_message_with_telegram_id(
                "assistant", final, tg_id, source="proactive",
            )
        else:
            db.append_message("assistant", final, source="proactive")
    except Exception:
        logger.exception(
            "morning_brief: append_message post-send failed (non-fatal)",
        )
    try:
        db.proactive_event_insert(
            source="morning_brief",
            pattern="ceremony",
            payload_json="{}",
            telegram_message_id=tg_id,
        )
    except Exception:
        logger.exception("morning_brief: proactive_event_insert failed (non-fatal)")
    cadence.record_ceremony_sent("morning_brief")
    db.runtime_set("last_morning_brief_sent",
                   datetime.now(UTC).isoformat())
    logger.info("morning_brief: sent (sources=%s)",
                ",".join(forecast["sources"].keys()))
    return True
