"""Emotional half-life. Tracks ``last_heavy_at + intensity + kind`` so Hikari's
mood doesn't snap back to neutral after a heavy moment in the prior turn.

Two pieces:
  - :func:`scan_inbound` runs deterministic regex over inbound user text. If a
    heavy-moment pattern matches, record a fresh affect state in runtime_state.
  - :func:`current_affect` reads the stored state and computes the *decayed*
    intensity for right-now. ``inject_affect_block`` formats it for the hook.

All thresholds and signal patterns live in
``config/engagement.yaml -> emotional_half_life``.
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import UTC, datetime

from storage import db

from . import config as cfg

logger = logging.getLogger(__name__)

_KEY = "affect_state"
_PATTERN_CACHE: list[re.Pattern[str]] | None = None


def _patterns() -> list[re.Pattern[str]]:
    global _PATTERN_CACHE
    if _PATTERN_CACHE is None:
        raw = cfg.get("emotional_half_life.heavy_moment_signals") or []
        _PATTERN_CACHE = [re.compile(p) for p in raw]
    return _PATTERN_CACHE


def reload_patterns() -> None:
    global _PATTERN_CACHE
    _PATTERN_CACHE = None


def _enabled() -> bool:
    return bool(cfg.get("emotional_half_life.enabled", True))


def _decay_hours() -> float:
    return float(cfg.get("emotional_half_life.decay_hours", 12.0))


def _min_intensity_to_inject() -> float:
    return float(cfg.get("emotional_half_life.min_intensity_to_inject", 0.15))


def scan_inbound(text: str) -> tuple[bool, str | None]:
    """Scan an inbound user message for heavy-moment signals.

    On match, write a fresh affect state (intensity = 1.0) to runtime_state and
    return ``(True, matched_pattern)``. On no match, leave existing state alone
    and return ``(False, None)``.
    """
    if not _enabled() or not text:
        return False, None
    for pat in _patterns():
        m = pat.search(text)
        if m:
            kind = _classify_kind(m.group(0))
            state = {
                "last_heavy_at": datetime.now(UTC).isoformat(),
                "intensity": 1.0,
                "kind": kind,
                "trigger": m.group(0)[:80],
            }
            db.runtime_set(_KEY, json.dumps(state))
            try:
                from agents.mode_dispatch import activate_comfort_mode
                activate_comfort_mode(trigger=m.group(0), kind=kind)
            except Exception:
                logger.warning("activate_comfort_mode failed (non-fatal)", exc_info=True)
            return True, m.group(0)
    return False, None


def _classify_kind(matched_text: str) -> str:
    """Map a matched signal fragment to one of the configured states."""
    t = matched_text.lower()
    if any(w in t for w in ("died", "dying", "funeral", "divorce", "broke up")):
        return "raw"
    if any(w in t for w in ("fired", "laid off", "panic", "scared", "terrified")):
        return "sharp"
    if any(w in t for w in ("crying", "cried")):
        return "quiet"
    if any(w in t for w in ("can't sleep", "haven't slept")):
        return "tired"
    return "quiet"


def current_affect() -> dict | None:
    """Return the affect state with decayed intensity, or None if expired/missing."""
    if not _enabled():
        return None
    raw = db.runtime_get(_KEY)
    if not raw:
        return None
    try:
        state = json.loads(raw)
    except (ValueError, TypeError):
        return None
    ts_iso = state.get("last_heavy_at")
    initial = float(state.get("intensity") or 0.0)
    if not ts_iso or initial <= 0:
        return None
    try:
        ts = datetime.fromisoformat(ts_iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None
    hours = (datetime.now(UTC) - ts).total_seconds() / 3600
    decay_hours = _decay_hours()
    # Exponential decay so half-life is ``decay_hours`` hours.
    decayed = initial * math.pow(0.5, hours / max(0.1, decay_hours))
    state["intensity"] = decayed
    return state


def inject_affect_block() -> str:
    """Hook helper. Returns a context block describing the current affect state,
    or empty string if intensity is below the inject threshold."""
    state = current_affect()
    if state is None:
        return ""
    intensity = float(state.get("intensity") or 0.0)
    if intensity < _min_intensity_to_inject():
        return ""
    kind = str(state.get("kind") or "quiet")
    pct = int(intensity * 100)
    return (
        "# emotional state (decayed from a heavy moment)\n"
        f"you are still ~{pct}% in [{kind}] from a heavy moment in a prior turn. "
        "let it color but don't announce it. don't perform recovery."
    )
