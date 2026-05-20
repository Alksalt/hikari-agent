"""Bridge-layer UX choreography — typing delays, false-start.

These are Telegram-specific patterns that make Hikari feel like a person texting,
not an instant-reply bot. They live in the bridge (not the agent) so they can
manipulate Telegram's typing indicator and pacing directly.

All thresholds / probabilities live in ``config/engagement.yaml``
under ``typing``, ``false_start``. No hardcoded values here.

State lives in storage.db.runtime_state so it persists across bot restarts:
  - false_start_used_date (YYYY-MM-DD)
"""

from __future__ import annotations

import random
from datetime import date

from storage import db

from . import config as cfg


def _typing() -> dict:
    return cfg.section("typing")


def _false_start() -> dict:
    return cfg.section("false_start")


# Re-exported for telegram_bridge's choreography wrapper.
def false_start_pause_sec() -> float:
    return float(_false_start().get("pause_sec", 2.5))


def false_start_resume_sec() -> float:
    return float(_false_start().get("resume_sec", 0.5))


def compute_typing_delay(text: str, mood: str) -> float:
    t = _typing()
    base = float(t.get("base_sec", 1.5)) + float(t.get("per_char", 0.04)) * len(text or "")
    base = min(base, float(t.get("cap_sec", 6.0)))
    mults = t.get("mood_multipliers") or {}
    return base * float(mults.get(mood, 1.0))


def should_false_start(text: str) -> bool:
    fs = _false_start()
    if len(text or "") < int(fs.get("min_text_length", 80)):
        return False
    if random.random() > float(fs.get("probability", 0.10)):
        return False
    today = date.today().isoformat()
    used_day = db.runtime_get("false_start_used_date")
    if used_day == today:
        return False
    db.runtime_set("false_start_used_date", today)
    return True


