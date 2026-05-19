"""Bridge-layer UX choreography — typing delays, false-start, ignore mechanic.

These are Telegram-specific patterns that make Hikari feel like a person texting,
not an instant-reply bot. They live in the bridge (not the agent) so they can
manipulate Telegram's typing indicator and pacing directly.

All thresholds / probabilities / action-line pools live in ``config/engagement.yaml``
under ``typing``, ``false_start``, ``ignore``. No hardcoded values here.

State lives in storage.db.runtime_state so it persists across bot restarts:
  - ignore_streak (int)
  - ignore_cooldown (int — remaining turns of cooldown after a max-streak break)
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


def _ignore() -> dict:
    return cfg.section("ignore")


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


def should_ignore(mood: str) -> tuple[bool, str | None]:
    """Returns (ignore, action_line_to_send). When ignore=True, the bridge sends
    only the action_line and does NOT call the agent."""
    ig = _ignore()
    cooldown = db.runtime_get_int("ignore_cooldown", 0)
    if cooldown > 0:
        db.runtime_set("ignore_cooldown", cooldown - 1)
        return False, None

    max_streak = int(ig.get("max_streak", 3))
    cooldown_turns = int(ig.get("cooldown_turns", 3))
    streak = db.runtime_get_int("ignore_streak", 0)
    if streak >= max_streak:
        db.runtime_set("ignore_streak", 0)
        db.runtime_set("ignore_cooldown", cooldown_turns)
        return False, None

    probs = ig.get("probability_by_mood") or {}
    prob = float(probs.get(mood, 0.10))
    if random.random() > prob:
        db.runtime_set("ignore_streak", 0)
        return False, None

    db.runtime_set("ignore_streak", streak + 1)
    lines = ig.get("action_lines") or ["[ignores]"]
    return True, random.choice(lines)
