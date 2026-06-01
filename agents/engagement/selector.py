"""Per-tick selector: pick the single highest-scoring candidate.

Scoring per architect spec §9.5:
  score = (novelty*0.4 + actionability*0.25 + confidence*0.2)
          * time_of_day_multiplier
          * mood_multiplier
          * (response_rate + 0.5)
          * (1 - recency_penalty)

Extended with a value_score (rubric §engagement.value_rubric) that must meet
per-source min_value_score to pass.  send_mode=="silent" sources are filtered
before scoring.  Bundle co-firing guard: two candidates in the same 60s tick
are detected; the second is silently dropped (not held — see below).

NOTE: cofire state is NOT written during select().  After a successful send,
the caller (sender.send) must call commit_cofire(source) to record the sent
source so future ticks can detect co-fires.  This keeps select() side-effect-free.
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
    "calendar_event_prep",
    "calendar_new_invite",
    "reminder_fire",
    "decision_resolve_due",
})
_EVENING_PREFERRED = frozenset({
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


def _source_send_mode(source: str) -> str:
    """Return send_mode for source from config (proactive|silent|observation). Default: proactive."""
    from agents import config as _cfg
    return str(_cfg.get(f"engagement.{source}.send_mode", "proactive"))


def _source_min_value_score(source: str) -> float:
    """Return min_value_score for source from config. Default: 0.0."""
    from agents import config as _cfg
    return float(_cfg.get(f"engagement.{source}.min_value_score", 0.0))


def _source_interruption_right(source: str) -> str:
    """Return interruption_right for source from config (low|medium|high). Default: low."""
    from agents import config as _cfg
    return str(_cfg.get(f"engagement.{source}.interruption_right", "low"))


def _value_score(candidate: TriggerCandidate, ctx: SimpleNamespace) -> float:
    """Compute a 0..1 value rubric score for a candidate.

    Inputs:
      anchor        — does the candidate reference a real, named event/object?
      user_value    — does the user benefit from receiving this?
      actionability — does it suggest a concrete next step?
      timing        — is the current time slot suitable?
      interruption_cost — subtracted based on interruption_right level

    Weights are read from engagement.value_rubric.weights in config.
    """
    from agents import config as _cfg

    weights: dict = _cfg.get("engagement.value_rubric.weights", {}) or {}
    w_anchor       = float(weights.get("anchor", 0.2))
    w_user_value   = float(weights.get("user_value", 0.3))
    w_actionability = float(weights.get("actionability", 0.2))
    w_timing       = float(weights.get("timing", 0.15))
    w_int_cost     = float(weights.get("interruption_cost", 0.15))

    cost_map: dict = _cfg.get("engagement.value_rubric.interruption_cost_map", {}) or {}
    ir = _source_interruption_right(candidate.source)
    interruption_cost = float(cost_map.get(ir, 0.05))

    # anchor: True if any ANCHOR_TOKEN_PATHS key is present in payload with a non-empty value
    from agents.engagement.guard import ANCHOR_TOKEN_PATHS
    anchor_paths = ANCHOR_TOKEN_PATHS.get(candidate.source, ())
    anchor = 1.0 if any(candidate.payload.get(p) for p in anchor_paths) else 0.0

    # user_value: proxy — novelty (novel to the user = high value) combined with confidence
    user_value = min(1.0, (candidate.novelty + candidate.confidence) / 2.0)

    # actionability: take directly from candidate's actionability field
    actionability = candidate.actionability

    # timing: 1.0 during preferred hours, 0.5 off-hours, 0.1 quiet hours
    h = ctx.now_local.hour
    from agents.proactive import _is_quiet_now
    try:
        if _is_quiet_now():
            timing = 0.1
        elif (8 <= h < 12) or (18 <= h < 23):
            timing = 1.0
        else:
            timing = 0.5
    except Exception:
        timing = 0.5

    score = (
        w_anchor * anchor
        + w_user_value * user_value
        + w_actionability * actionability
        + w_timing * timing
        - w_int_cost * interruption_cost
    )
    return max(0.0, min(1.0, score))


# ---------------------------------------------------------------------------
# Bundle co-firing guard state (in-process, per-tick)
# ---------------------------------------------------------------------------

# Tracks the last candidate that was selected and its send timestamp ISO string.
# Written to runtime_state so it survives across ticks.
_COFIRE_KEY = "proactive_last_selected_at"
_COFIRE_SOURCE_KEY = "proactive_last_selected_source"
_COFIRE_WINDOW_SEC = 60        # same-tick window


def _get_cofire_state() -> tuple[str | None, str | None]:
    """Return (last_selected_iso, last_source) from runtime_state."""
    try:
        from storage import db as _db
        return _db.runtime_get(_COFIRE_KEY), _db.runtime_get(_COFIRE_SOURCE_KEY)
    except Exception:
        return None, None


def _set_cofire_state(iso: str, source: str) -> None:
    try:
        from storage import db as _db
        _db.runtime_set(_COFIRE_KEY, iso)
        _db.runtime_set(_COFIRE_SOURCE_KEY, source)
    except Exception:
        logger.exception("_set_cofire_state: failed to write runtime state")


def commit_cofire(source: str) -> None:
    """Record that *source* was successfully sent, for use by the cofire guard.

    Must be called by sender.send() AFTER a successful send (row id exists).
    select() is intentionally side-effect-free w.r.t. cofire state; only a
    confirmed delivery updates the window so dropped/failed candidates do not
    consume the cofire slot.
    """
    _set_cofire_state(datetime.now(UTC).isoformat(), source)


def _cofire_guard(
    best: TriggerCandidate,
    second: TriggerCandidate | None,
) -> TriggerCandidate:
    """Apply the 60s co-firing guard (read-only — no state mutations).

    If best fires within 60s of the last committed candidate (per commit_cofire),
    and a second candidate exists, log the co-fire event.  The second candidate is
    silently dropped at this tick.

    NOTE: cofire hold was write-only/never drained — removed; revisit if a real
    2-slot cofire is needed (e.g. drain at tick start, deliver held candidate before
    scoring new ones).

    Cofire state is NOT written here.  Call commit_cofire(source) from sender.send
    AFTER a successful send so only actually-delivered messages affect the window.
    """
    last_iso, last_source = _get_cofire_state()

    if last_iso:
        try:
            last_dt = datetime.fromisoformat(last_iso)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=UTC)
            age_sec = (datetime.now(UTC) - last_dt).total_seconds()
            if age_sec < _COFIRE_WINDOW_SEC and second is not None:
                logger.info(
                    "selector: co-fire detected (gap=%.1fs, last=%s) — dropping %s",
                    age_sec, last_source, second.source,
                )
        except (ValueError, TypeError):
            pass

    return best


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
    passes send_mode and value_score filters, or None if nothing qualifies.

    Extensions vs original:
    - Filters out sources with send_mode == "silent".
    - Computes value_score per candidate; filters below per-source min_value_score.
    - Applies bundle co-firing guard on the top-2 finalists.
    """
    if not candidates:
        return None
    enabled_sources: set[str] = ctx.enabled_sources
    snoozed: set[str] = _snoozed_sources()
    if "all" in snoozed:
        return None
    pool_caps: dict[str, bool] = ctx.pool_caps  # pool_name -> bool (can send)

    scored: list[tuple[float, float, TriggerCandidate]] = []
    for c in candidates:
        if c.source not in enabled_sources:
            continue
        if c.source in snoozed:
            continue
        if not pool_caps.get(c.pool, False):
            continue
        # send_mode filter: silent sources never send
        if _source_send_mode(c.source) == "silent":
            logger.debug("selector: %s filtered — send_mode=silent", c.source)
            continue
        s = score(c, ctx)
        vs = _value_score(c, ctx)
        min_vs = _source_min_value_score(c.source)
        if vs < min_vs:
            logger.debug(
                "selector: %s filtered — value_score=%.3f < min_value_score=%.3f",
                c.source, vs, min_vs,
            )
            continue
        scored.append((s, vs, c))

    if not scored:
        return None

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, _best_vs, best = scored[0]
    if best_score <= 0:
        return None

    second: TriggerCandidate | None = scored[1][2] if len(scored) >= 2 else None
    return _cofire_guard(best, second)
