"""Per-tick selector: pick the single highest-scoring candidate.

Scoring per architect spec §9.5:
  score = (novelty*0.4 + actionability*0.25 + confidence*0.2)
          * time_of_day_multiplier
          * mood_multiplier
          * (response_rate + 0.5)
          * (1 - recency_penalty)
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

from agents.engagement.triggers import TriggerCandidate

logger = logging.getLogger(__name__)


def _snoozed_sources() -> set[str]:
    """Return the set of source ids that are currently snoozed.

    Reads ``proactive_snooze_until`` from runtime_state — a JSON map of
    {source: iso_timestamp}.  Entries whose timestamp is in the past are
    considered expired and excluded from the returned set.
    """
    try:
        from storage import db as _db
        raw = _db.runtime_get("proactive_snooze_until")
        if not raw:
            return set()
        snooze_map: dict[str, str] = json.loads(raw)
        now = datetime.now(UTC)
        snoozed: set[str] = set()
        for source, iso in snooze_map.items():
            try:
                until = datetime.fromisoformat(iso)
                if until.tzinfo is None:
                    until = until.replace(tzinfo=UTC)
                if now < until:
                    snoozed.add(source)
            except (ValueError, TypeError):
                continue
        return snoozed
    except Exception:
        logger.exception("_snoozed_sources: error reading snooze map (returning empty)")
        return set()


# Sources that should be dampened after quiet hours vs preferred in the
# morning when the user is fresh.
_MORNING_PREFERRED = frozenset({
    "gmail_unread_threshold",
    "gmail_important_thread",
    "calendar_event_prep",
    "calendar_new_invite",
    "reminder_fire",
    "decision_resolve_due",
})
_EVENING_PREFERRED = frozenset({
    "readwise_daily_review",
    "callback_episode",
    "weirdly_good_mood_leak",
})
def _time_of_day_multiplier(now_local: datetime, source: str) -> float:
    """Boost morning sources in the morning, evening sources in the evening,
    and suppress everything during quiet hours (from engagement.yaml)."""
    from agents.proactive import _is_quiet_now
    if _is_quiet_now():
        return 0.1  # heavy suppression — cadence governor also blocks, this is a safety layer
    h = now_local.hour
    if 8 <= h < 12 and source in _MORNING_PREFERRED:
        return 1.3
    if 18 <= h < 23 and source in _EVENING_PREFERRED:
        return 1.2
    return 1.0


def _mood_multiplier(mood: str, pattern: str) -> float:
    """Irritable mood suppresses questions; weirdly_good boosts them.
    Tired mood suppresses all proactives slightly."""
    mood = (mood or "focused").lower().strip()
    if mood == "irritable":
        if pattern == "question":
            return 0.4
        return 0.7
    if mood == "tired":
        return 0.8
    if mood == "weirdly good":
        if pattern == "question":
            return 1.2
        return 1.1
    return 1.0  # focused


_TIER_MULTIPLIER = {1: 1.5, 2: 1.0, 3: 0.6}


def _hard_interval_blocked(source: str, last_send_per_source: dict[str, str]) -> bool:
    """Return True if source fired more recently than its configured min_interval_minutes."""
    from agents import config as _cfg
    min_minutes = float(_cfg.get(f"engagement.{source}.min_interval_minutes", 0))
    if min_minutes <= 0:
        return False
    iso = last_send_per_source.get(source)
    if not iso:
        return False
    try:
        last = datetime.fromisoformat(iso)
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return False
    age_minutes = (datetime.now(UTC) - last).total_seconds() / 60
    return age_minutes < min_minutes


def _priority_tier_multiplier(source: str) -> float:
    """Return tier multiplier (1=1.5x, 2=1.0x, 3=0.6x) from config."""
    from agents import config as _cfg
    tier = int(_cfg.get(f"engagement.{source}.priority_tier", 2))
    return _TIER_MULTIPLIER.get(tier, 1.0)


def _recency_penalty(source: str, last_send_per_source: dict[str, str]) -> float:
    """Returns a 0..1 soft penalty based on how recently this source sent.
    Linear decay: 0 penalty after 24h, full 0.9 penalty if sent <1h ago.
    Applied after the hard interval gate — provides a gradient for sources
    that passed the hard gate but still sent recently."""
    iso = last_send_per_source.get(source)
    if not iso:
        return 0.0
    try:
        last = datetime.fromisoformat(iso)
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return 0.0
    age_hours = (datetime.now(UTC) - last).total_seconds() / 3600
    if age_hours >= 24:
        return 0.0
    return max(0.0, 0.9 * (1 - age_hours / 24))


def score(candidate: TriggerCandidate, ctx: SimpleNamespace) -> float:
    if _hard_interval_blocked(candidate.source, ctx.last_send_per_source):
        return 0.0
    s = (
        candidate.novelty * 0.4
        + candidate.actionability * 0.25
        + candidate.confidence * 0.2
    )
    s *= _time_of_day_multiplier(ctx.now_local, candidate.source)
    s *= _mood_multiplier(ctx.mood, candidate.pattern)
    s *= ctx.source_response_rate.get(candidate.source, 0.5) + 0.5
    s *= 1 - _recency_penalty(candidate.source, ctx.last_send_per_source)
    s *= _priority_tier_multiplier(candidate.source)
    return s


def select(candidates: list[TriggerCandidate], ctx: SimpleNamespace) -> TriggerCandidate | None:
    """Return the highest-scoring enabled candidate that fits within pool caps,
    or None if nothing qualifies."""
    if not candidates:
        return None
    enabled_sources: set[str] = ctx.enabled_sources
    snoozed: set[str] = _snoozed_sources()
    if "all" in snoozed:
        return None
    pool_caps: dict[str, bool] = ctx.pool_caps  # pool_name -> bool (can send)

    scored: list[tuple[float, TriggerCandidate]] = []
    for c in candidates:
        if c.source not in enabled_sources:
            continue
        if c.source in snoozed:
            continue
        if not pool_caps.get(c.pool, False):
            continue
        s = score(c, ctx)
        scored.append((s, c))

    if not scored:
        return None

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best = scored[0]
    return best if best_score > 0 else None
