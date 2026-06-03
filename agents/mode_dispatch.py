"""Mode dispatch — comfort_mode + anger_mode runtime flag setters/readers.

Triggers wired from:
- agents/affect.py — comfort_mode on distress signal scan
- agents/telegram_bridge.py — anger_mode on rude_repeat >= threshold

Decay: comfort persists N turns past last distress signal (config).
Anger releases on softening pattern / session boundary / 24h timeout.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta

from agents import config as cfg
from storage import db

logger = logging.getLogger(__name__)


_COMFORT_KEY = "comfort_mode_state"
_ANGER_KEY = "anger_mode_state"
_INTIMACY_KEY = "intimacy_mode_state"


_INTIMACY_CUE_PATTERNS: list[re.Pattern] | None = None


def _intimacy_cue_patterns() -> list[re.Pattern]:
    global _INTIMACY_CUE_PATTERNS
    if _INTIMACY_CUE_PATTERNS is None:
        raw = cfg.get("mode_flags.intimacy.cue_patterns") or []
        out: list[re.Pattern] = []
        for p in raw:
            try:
                out.append(re.compile(p))
            except re.error:
                logger.warning("invalid intimacy cue pattern: %s", p)
        _INTIMACY_CUE_PATTERNS = out
    return _INTIMACY_CUE_PATTERNS


def _compile_softening_patterns() -> list[re.Pattern]:
    raw = cfg.get("mode_flags.anger.softening_patterns") or [
        r"(?i)\b(sorry|sry|my bad|i apologize|didn't mean|that came out wrong)\b"
    ]
    out = []
    for p in raw:
        try:
            out.append(re.compile(p))
        except re.error:
            logger.warning("invalid softening pattern: %s", p)
    return out


_SOFTENING_PATTERNS: list[re.Pattern] | None = None


def _softening_patterns() -> list[re.Pattern]:
    global _SOFTENING_PATTERNS
    if _SOFTENING_PATTERNS is None:
        _SOFTENING_PATTERNS = _compile_softening_patterns()
    return _SOFTENING_PATTERNS


def activate_comfort_mode(trigger: str, kind: str = "distress") -> None:
    if not bool(cfg.get("mode_flags.comfort.enabled", True)):
        return
    persist_turns = int(cfg.get("mode_flags.comfort.persist_turns", 2))
    state = {
        "activated_at": datetime.now(UTC).isoformat(),
        "trigger": trigger[:100],
        "turns_remaining": persist_turns,
        "kind": kind,
    }
    db.runtime_set(_COMFORT_KEY, json.dumps(state))
    logger.info("comfort_mode activated: trigger=%r kind=%s", trigger[:50], kind)


def activate_anger_mode(trigger: str) -> None:
    if not bool(cfg.get("mode_flags.anger.enabled", True)):
        return
    timeout_hours = int(cfg.get("mode_flags.anger.timeout_hours", 24))
    state = {
        "activated_at": datetime.now(UTC).isoformat(),
        "trigger": trigger[:100],
        "expires_at": (datetime.now(UTC) + timedelta(hours=timeout_hours)).isoformat(),
    }
    db.runtime_set(_ANGER_KEY, json.dumps(state))
    logger.info("anger_mode activated: trigger=%r", trigger[:50])


def activate_intimacy_mode(trigger: str = "owner") -> None:
    """Arm the on-demand intimacy register. Owner-invoked (/closer or a cue
    phrase). Relaxes the mood gate for ``persist_turns`` turns regardless of
    today's mood; the denial layer stays on (see character-voice/INTIMATE.md)."""
    if not bool(cfg.get("mode_flags.intimacy.enabled", True)):
        return
    persist_turns = int(cfg.get("mode_flags.intimacy.persist_turns", 6))
    state = {
        "activated_at": datetime.now(UTC).isoformat(),
        "trigger": trigger[:100],
        "turns_remaining": persist_turns,
    }
    db.runtime_set(_INTIMACY_KEY, json.dumps(state))
    logger.info("intimacy_mode activated: trigger=%r turns=%d", trigger[:50], persist_turns)


def deactivate_intimacy_mode() -> None:
    """Clear intimacy_mode immediately (e.g. ``/closer off``)."""
    db.runtime_set(_INTIMACY_KEY, None)


def scan_intimacy_cue(text: str) -> bool:
    """Return True (and arm intimacy_mode) if inbound text is a whole-message
    closer cue. Patterns are anchored in config so task talk never trips it."""
    if not text:
        return False
    for p in _intimacy_cue_patterns():
        if p.search(text):
            activate_intimacy_mode(trigger=text.strip()[:80])
            return True
    return False


def current_comfort_mode() -> dict | None:
    raw = db.runtime_get(_COMFORT_KEY)
    if not raw:
        return None
    try:
        state = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(state, dict):
        return None
    if int(state.get("turns_remaining", 0)) <= 0:
        db.runtime_set(_COMFORT_KEY, None)
        return None
    return state


def current_anger_mode() -> dict | None:
    raw = db.runtime_get(_ANGER_KEY)
    if not raw:
        return None
    try:
        state = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(state, dict):
        return None
    exp = state.get("expires_at", "")
    if exp:
        try:
            exp_dt = datetime.fromisoformat(exp)
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=UTC)
            if datetime.now(UTC) > exp_dt:
                db.runtime_set(_ANGER_KEY, None)
                return None
        except (ValueError, TypeError):
            pass
    return state


def current_intimacy_mode() -> dict | None:
    raw = db.runtime_get(_INTIMACY_KEY)
    if not raw:
        return None
    try:
        state = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(state, dict):
        return None
    if int(state.get("turns_remaining", 0)) <= 0:
        db.runtime_set(_INTIMACY_KEY, None)
        return None
    return state


def _decrement_turn_key(key: str) -> None:
    raw = db.runtime_get(key)
    if not raw:
        return
    try:
        state = json.loads(raw)
    except (ValueError, TypeError):
        db.runtime_set(key, None)
        return
    if not isinstance(state, dict):
        db.runtime_set(key, None)
        return
    remaining = int(state.get("turns_remaining", 0)) - 1
    if remaining <= 0:
        db.runtime_set(key, None)
    else:
        state["turns_remaining"] = remaining
        db.runtime_set(key, json.dumps(state))


def decrement_comfort_turn() -> None:
    """Decrement turns_remaining on comfort mode by 1. Clears when reaches 0."""
    _decrement_turn_key(_COMFORT_KEY)


def decrement_intimacy_turn() -> None:
    """Decrement turns_remaining on intimacy mode by 1. Clears when reaches 0."""
    _decrement_turn_key(_INTIMACY_KEY)


def scan_softening(text: str) -> bool:
    """Return True if user text contains a softening pattern. Clears anger if so."""
    if not text:
        return False
    for p in _softening_patterns():
        if p.search(text):
            db.runtime_set(_ANGER_KEY, None)
            return True
    return False


def clear_on_session_boundary() -> None:
    """Called when SDK session_id rotates.

    Clears session-scoped mode flags that must not leak across session
    boundaries: comfort_mode_state and anger_mode_state.

    Does NOT clear cross-session state (``prior_session_heavy``, relationship
    stage, facts, etc.) — that state is intentionally written at the boundary
    and consumed by the next session.

    Behaviour is config-gated per mode via
    ``mode_flags.{comfort,anger}.clear_on_session_boundary`` (both default True).
    Safe to call when no modes are active (idempotent). Never raises.
    """
    try:
        if bool(cfg.get("mode_flags.comfort.clear_on_session_boundary", True)):
            db.runtime_set(_COMFORT_KEY, None)
    except Exception:
        logger.exception("clear_on_session_boundary: failed to clear comfort_mode (non-fatal)")
    try:
        if bool(cfg.get("mode_flags.anger.clear_on_session_boundary", True)):
            db.runtime_set(_ANGER_KEY, None)
    except Exception:
        logger.exception("clear_on_session_boundary: failed to clear anger_mode (non-fatal)")
    try:
        if bool(cfg.get("mode_flags.intimacy.clear_on_session_boundary", True)):
            db.runtime_set(_INTIMACY_KEY, None)
    except Exception:
        logger.exception("clear_on_session_boundary: failed to clear intimacy_mode (non-fatal)")
