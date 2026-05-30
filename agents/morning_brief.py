"""Phase 10: daily morning weather brief — proactive at 06:00 local.

Pipeline:
  1. Gate: core_block 'morning_brief_status' must not be 'disabled'
  2. Resolve location: most recent Telegram share (any age) -> HOME_LAT/LON
  3. Fetch multi-source forecast via tools/weather.py
  4. Optionally fetch HuggingFace daily papers filtered by interests pool
  5. Build prompt, call run_proactive -> Hikari writes in voice
  6. send_text the result
"""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from agents import config as cfg
from agents.injection_guard import wrap_untrusted
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

# Path to the interests pool config, relative to the repo root.
_INTERESTS_POOL_PATH = Path(__file__).parent.parent / "config" / "hikari_interests_pool.yaml"


async def _fetch_hf_daily_papers(limit: int = 20) -> list[dict]:
    """Fetch today's HuggingFace Daily Papers. No auth. Returns list of dicts
    with keys: title, summary, url. Empty list on any failure."""
    url = "https://huggingface.co/api/daily_papers"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params={"limit": limit})
        if resp.status_code != 200:
            logger.warning("hf_papers: HTTP %s", resp.status_code)
            return []
        data = resp.json()
        if not isinstance(data, list):
            return []
        out: list[dict] = []
        for item in data:
            paper = item.get("paper", {}) if isinstance(item, dict) else {}
            title = paper.get("title", "").strip()
            summary = paper.get("summary", "").strip()
            arxiv_id = paper.get("id", "").strip()
            paper_url = f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else ""
            if title and summary:
                out.append({"title": title, "summary": summary, "url": paper_url})
        return out
    except Exception:
        logger.exception("hf_papers: fetch failed (non-fatal)")
        return []


def _load_interests_keywords() -> list[str]:
    """Load keyword strings from hikari_interests_pool.yaml for paper matching.

    Extracts the ``title`` field from every interests entry as the keyword
    pool, plus any bare words ≥3 chars found in those titles. Returns a
    deduplicated, lowercased list suitable for ``in`` substring checks.
    """
    try:
        if not _INTERESTS_POOL_PATH.exists():
            return []
        raw = _INTERESTS_POOL_PATH.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
        if not isinstance(data, dict):
            return []
        entries = data.get("interests", [])
        if not isinstance(entries, list):
            return []
        keywords: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            title = entry.get("title", "")
            if title:
                # Add the full title (lowercased) as a phrase.
                keywords.add(title.lower())
                # Also add individual meaningful words (≥4 chars, skip stopwords).
                _STOPWORDS = {"the", "and", "for", "was", "are", "with", "this",
                              "that", "from", "have", "not", "been", "they",
                              "will", "than", "also", "into", "more"}
                for word in title.lower().split():
                    word = word.strip("()[]—–-.,:")
                    if len(word) >= 4 and word not in _STOPWORDS:
                        keywords.add(word)
        return sorted(keywords)
    except Exception:
        logger.exception("hf_papers: failed to load interests pool (non-fatal)")
        return []


def _filter_papers_by_interests(
    papers: list[dict],
    interests: list[str],
    max_results: int = 2,
) -> list[dict]:
    """Return papers whose title+summary contains any interest keyword
    (case-insensitive). Caps at max_results."""
    if not interests or not papers:
        return []
    keywords = [k.lower() for k in interests if k and len(k) >= 3]
    matched: list[dict] = []
    for p in papers:
        hay = (p.get("title", "") + " " + p.get("summary", "")).lower()
        if any(kw in hay for kw in keywords):
            matched.append(p)
            if len(matched) >= max_results:
                break
    return matched


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


def _build_prompt(
    forecast: dict[str, Any],
    label: str | None,
    papers: list[dict] | None = None,
) -> str:
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
            cond_part = f" {cond}" if cond else ""
            precip_part = f" rain {precip}%" if precip else ""
            return f"  {name}: {t}°C{cond_part}{precip_part}"
        window_text = (
            "\n  windows:\n"
            + _win(morning, "morning") + "\n"
            + _win(midday, "midday") + "\n"
            + _win(evening, "evening")
        )

    papers_section = ""
    if papers:
        lines = "\n".join(
            f"  - {wrap_untrusted('morning_brief:hf_paper_title', p['title'])}"
            + (f" — {p['url']}" if p.get("url") else "")
            for p in papers
        )
        papers_section = (
            f"\n\npapers worth a look (≤2, append after weather if you choose to "
            f"mention them — only if they fit the beat naturally; silence is fine):\n"
            f"{lines}"
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
        f"{disagreement_note}"
        f"{papers_section}\n\n"
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

    # HuggingFace daily papers — non-fatal; brief ships without them on failure.
    papers: list[dict] = []
    if bool(cfg.get("morning_brief.hf_papers_enabled", True)):
        fetch_limit = int(cfg.get("morning_brief.hf_papers_fetch_limit", 20))
        max_results = int(cfg.get("morning_brief.hf_papers_max_results", 2))
        raw_papers = await _fetch_hf_daily_papers(limit=fetch_limit)
        if raw_papers:
            keywords = _load_interests_keywords()
            papers = _filter_papers_by_interests(raw_papers, keywords, max_results=max_results)
            logger.info(
                "hf_papers: fetched=%d matched=%d",
                len(raw_papers),
                len(papers),
            )

    prompt = _build_prompt(forecast, label, papers=papers or None)
    try:
        # Look up via module globals so tests can monkeypatch
        # ``morning_brief.run_proactive``.
        text = (await run_proactive(prompt)).strip()
    except Exception:
        logger.exception("morning_brief: run_proactive failed")
        return False
    if not text or text.upper().startswith("NO_MESSAGE"):
        return False
    from agents.proactive_gate import reserve_and_send
    today = datetime.now(UTC).date()
    result = await reserve_and_send(
        send_text_fn=send_text,
        producer_id="morning_brief",
        pattern="ceremony",
        text=text,
        payload_json="{}",
        candidate={
            "anchor": today.isoformat(),
            "why_now": f"morning brief for {today}",
            "suggested_action": "reply to engage",
            "confidence": 0.9,
            "controls": {},
            "data_checked": ["briefings"],
        },
    )
    if result.status != "sent":
        logger.info("morning_brief: skipped (%s)", result.reason)
        return False
    cadence.record_ceremony_sent("morning_brief")
    db.runtime_set("last_morning_brief_sent",
                   datetime.now(UTC).isoformat())
    logger.info("morning_brief: sent (sources=%s)",
                ",".join(forecast["sources"].keys()))
    return True
