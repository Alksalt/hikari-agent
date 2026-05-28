"""Cross-session emotional half-life.

Detects whether the prior session ended "heavy" and arms a softer-opener
flag for the next session's first few exchanges.

Three public functions:

- :func:`detect_session_heaviness` — returns the first matched trigger name
  or None. Only triggers with live detectors fire; unimplemented ones return
  None and never arm.
- :func:`arm_if_heavy` — called at session-rotation boundary; if a trigger
  is detected, writes ``prior_session_heavy`` to runtime_state.
- :func:`consume_softer_opener` — reads ``prior_session_heavy`` and returns
  it (clearing after decay) so the hooks layer can inject the softer-opener
  block.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from agents import config as cfg
from storage import db

logger = logging.getLogger(__name__)


def detect_session_heaviness() -> str | None:
    """Return the first matched trigger name, or None.

    Trigger map (only instrumented detectors can fire):
    - l3_refusal   → mode_dispatch.current_anger_mode() is not None
    - l4_silence   → bool(db.runtime_get("silenced_until_msg_id"))
    - repair_move  → NOT instrumented; always returns None this phase
    - overt_warmth_event → NOT instrumented; always returns None this phase

    Plus an always-checked heavy-emotional proxy:
    - current affect intensity >= emotional_half_life.min_intensity_to_inject
      OR current_comfort_mode() is active → synthetic "heavy_emotional"
    """
    triggers = cfg.get("emotional_half_life.cross_session.triggers") or []

    for trigger in triggers:
        matched = _check_trigger(trigger)
        if matched:
            return trigger

    try:
        from agents import affect, mode_dispatch
        aff = affect.current_affect()
        intensity_threshold = float(
            cfg.get("emotional_half_life.min_intensity_to_inject", 0.15)
        )
        if aff is not None and float(aff.get("intensity") or 0.0) >= intensity_threshold:
            return "heavy_emotional"
        if mode_dispatch.current_comfort_mode() is not None:
            return "heavy_emotional"
    except Exception:
        logger.exception("detect_session_heaviness: heavy-emotional proxy check failed (non-fatal)")

    return None


def _check_trigger(trigger: str) -> bool:
    """Return True if the named trigger is currently active. Unimplemented
    triggers always return False so they never fire."""
    try:
        if trigger == "l3_refusal":
            from agents import mode_dispatch
            return mode_dispatch.current_anger_mode() is not None
        if trigger == "l4_silence":
            return bool(db.runtime_get("silenced_until_msg_id"))
        if trigger in ("repair_move", "overt_warmth_event"):
            return False
    except Exception:
        logger.exception("_check_trigger %r failed (non-fatal)", trigger)
    return False


def arm_if_heavy() -> None:
    """Called at session-rotation boundary.

    If cross_session is enabled and a heaviness trigger is detected, writes
    ``prior_session_heavy`` to runtime_state (idempotent — overwrites any
    stale value). If NO trigger fires, CLEARS any stale flag: this runs once
    per rotation and reflects "was the just-ended session heavy?", so a calm
    session must wipe a previous heavy session's flag — otherwise the softer
    opener leaks into a later, non-heavy session.
    """
    if not cfg.get("emotional_half_life.cross_session.enabled", True):
        return
    try:
        trigger = detect_session_heaviness()
        if trigger is None:
            db.runtime_set("prior_session_heavy", None)
            return
        payload = json.dumps({"trigger": trigger, "ts": datetime.now(UTC).isoformat()})
        db.runtime_set("prior_session_heavy", payload)
        logger.info("cross_session: armed prior_session_heavy trigger=%r", trigger)
    except Exception:
        logger.exception("arm_if_heavy failed (non-fatal)")


def consume_softer_opener() -> dict | None:
    """Return the armed prior_session_heavy state if within decay_turns, else None.

    Reads ``session_turn_count`` (resets to 0 at session rotation) and
    ``decay_turns`` from config. If the session turn count is within decay,
    returns the parsed state dict. Past decay — clears the flag and returns None.
    If not armed — returns None.
    """
    raw = db.runtime_get("prior_session_heavy")
    if not raw:
        return None
    try:
        state = json.loads(raw)
    except (ValueError, TypeError):
        db.runtime_set("prior_session_heavy", None)
        return None

    # Wall-clock backstop: if a flag was armed long ago (e.g. an intervening
    # short session that never advanced session_turn_count past decay_turns to
    # clear it), expire it by age so it can't re-fire for a much-later session.
    armed_ts = state.get("ts")
    if armed_ts:
        try:
            armed_dt = datetime.fromisoformat(armed_ts)
            if armed_dt.tzinfo is None:
                armed_dt = armed_dt.replace(tzinfo=UTC)
            max_age_h = float(cfg.get("emotional_half_life.cross_session.max_arm_age_hours", 36))
            if (datetime.now(UTC) - armed_dt).total_seconds() > max_age_h * 3600:
                db.runtime_set("prior_session_heavy", None)
                return None
        except (ValueError, TypeError):
            db.runtime_set("prior_session_heavy", None)
            return None

    decay_turns = int(cfg.get("emotional_half_life.cross_session.decay_turns", 5))
    session_turn = db.runtime_get_int("session_turn_count", 0)

    if session_turn <= decay_turns:
        return state

    db.runtime_set("prior_session_heavy", None)
    return None
