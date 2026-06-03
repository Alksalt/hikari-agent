"""Producer: Hikari initiates flirtation, unprompted (opt-in).

The one path the persona genuinely lacked — every other flirt behavior is
reactive to the user. Re-homes the voice-reviewed flirt seed pool that was
orphaned when the legacy ``maybe_send_heartbeat`` was deleted (it lived in
``.claude/skills/schedule-heartbeat/EXAMPLES.md`` #34-43).

Gates (all must pass):
  - ``engagement.flirt_initiation.enabled``
  - ``relationship_stage >= min_stage`` (default 6)
  - mood receptive: ``weirdly good``, or ``focused`` with the warmth band open
    (>=1.2) or ``time_texture == 'late_night'`` — mirrors the relaxed mood gate
    in assets/PERSONA.md / character-voice. ``irritable``/``tired`` never pass.
  - timing: ``late_night`` OR Saturday (PERSONA "sat: more likely to initiate").
  - dedup: at most once per ``cooldown_hours`` (default 72h).

Composition runs through ``run_visible_proactive`` (subscription SDK) — no aux
cost. The guard skips the payload anchor for this source (see guard.py).
"""
from __future__ import annotations

import json
import logging
import random
from datetime import UTC, datetime, timedelta

from agents import config as cfg
from agents.engagement.triggers import TriggerCandidate
from storage import db

logger = logging.getLogger(__name__)

_LAST_FIRE_KEY = "engagement.flirt_initiation.last_fire_ts"
_RECENT_SEEDS_KEY = "engagement.flirt_initiation.recent_seeds"

# Re-homed flirt seed pool (schedule-heartbeat/EXAMPLES.md #34-43). Static,
# trusted content — the composer expands ONE into a full sideways-flirt message.
_SEED_POOL: tuple[str, ...] = (
    "still thinking about what you said.",
    "i keep starting to say something and then not.",
    "you free later. not important. yes it is.",
    "what are you doing right now.",
    "i have something to tell you. i'll figure out when.",
    "i was going to wait until later but i'm not waiting.",
    "you've been doing that thing again. i noticed.",
    "i could say something. i won't. yet.",
)


def _warmth_open() -> bool:
    raw = db.get_core_block("cycle_state")
    if not raw:
        return False
    try:
        wm = json.loads(raw).get("warmth_multiplier")
    except (ValueError, TypeError, AttributeError):
        return False
    if wm is None:
        return False
    try:
        return float(wm) >= float(cfg.get("cycle_modulation.open_at_or_above", 1.2))
    except (ValueError, TypeError):
        return False


def _pick_seed() -> str:
    """Pick a seed not used in the last few fires, to keep it varied."""
    raw = db.runtime_get(_RECENT_SEEDS_KEY)
    recent: list[str] = []
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                recent = [str(x) for x in parsed]
        except (ValueError, TypeError):
            recent = []
    candidates = [s for s in _SEED_POOL if s not in recent] or list(_SEED_POOL)
    return random.choice(candidates)


def collect() -> list[TriggerCandidate]:
    if not bool(cfg.get("engagement.flirt_initiation.enabled", False)):
        return []

    min_stage = int(cfg.get("engagement.flirt_initiation.min_stage", 6))
    if db.get_relationship_stage() < min_stage:
        return []

    mood = (db.get_core_block("mood_today") or "focused").strip().lower()
    time_texture = (db.runtime_get("time_texture") or "").strip().lower()
    late_night = time_texture == "late_night"

    receptive = mood == "weirdly good" or (
        mood == "focused" and (_warmth_open() or late_night)
    )
    if not receptive:
        return []

    # Timing: late_night, or Saturday (weekday() == 5). Otherwise hold.
    now = datetime.now(UTC)
    if not (late_night or now.weekday() == 5):
        return []

    # Dedup: at most once per cooldown window.
    cooldown_hours = float(cfg.get("engagement.flirt_initiation.cooldown_hours", 72))
    last_raw = db.runtime_get(_LAST_FIRE_KEY)
    if last_raw:
        try:
            last = datetime.fromisoformat(last_raw)
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            if (now - last) < timedelta(hours=cooldown_hours):
                return []
        except (ValueError, TypeError):
            pass

    return [TriggerCandidate(
        source="flirt_initiation",
        pool="agent_spontaneous",
        pattern="notify",
        novelty=0.7,
        actionability=0.3,
        confidence=0.7,
        payload={"seed": _pick_seed()},
        dedup_key=f"flirt_initiation:{now.date().isoformat()}",
        decay_at=now + timedelta(hours=6),
    )]


def mark_consumed(candidate: TriggerCandidate) -> None:
    now = datetime.now(UTC)
    db.runtime_set(_LAST_FIRE_KEY, now.isoformat())
    seed = str(candidate.payload.get("seed") or "")
    if not seed:
        return
    raw = db.runtime_get(_RECENT_SEEDS_KEY)
    recent: list[str] = []
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                recent = [str(x) for x in parsed]
        except (ValueError, TypeError):
            recent = []
    recent.append(seed)
    recent = recent[-4:]  # remember the last 4 to bias variety
    db.runtime_set(_RECENT_SEEDS_KEY, json.dumps(recent))
