"""Proactive cadence governor + soft-scarcity beat.

Two pieces of state, both in runtime_state:

  - ``proactive_log_v1`` — JSON list of ISO timestamps of sent proactive
    messages over the rolling 7-day window. Pruned on every read.
  - ``scarcity_skip_until`` — ISO timestamp. While ``now < scarcity_skip_until``,
    eligible heartbeat windows are silently skipped (the "she went quiet" beat).

The cadence governor caps total proactives per 7d AND requires each candidate
to declare a justified source (``open_loop`` / ``pattern_observation`` /
``lexicon_callback`` / ``noticed_change`` / ``recent_episode_callback`` /
``calendar_event`` / ``reengage_silence``). Without a source, the heartbeat is
vetoed even if under the cap.

All caps and source lists come from ``config/engagement.yaml -> cadence_governor``
and ``soft_scarcity``.
"""

from __future__ import annotations

import json
import logging
import random
from datetime import UTC, datetime, timedelta

from storage import db

from . import config as cfg

logger = logging.getLogger(__name__)

_LOG_KEY = "proactive_log_v1"
_SKIP_UNTIL_KEY = "scarcity_skip_until"
_LAST_SKIP_KEY = "scarcity_last_skip_at"


# ---------- cadence governor ----------

def _governor_enabled() -> bool:
    return bool(cfg.get("cadence_governor.enabled", True))


def _max_per_7d() -> int:
    return int(cfg.get("cadence_governor.max_proactive_per_7d", 4))


def _allowed_sources() -> set[str]:
    raw = cfg.get("cadence_governor.allowed_trigger_sources") or []
    return set(raw)


def _read_log() -> list[str]:
    raw = db.runtime_get(_LOG_KEY) or ""
    try:
        data = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    cutoff = datetime.now(UTC) - timedelta(days=7)
    out: list[str] = []
    for ts_iso in data:
        try:
            ts = datetime.fromisoformat(str(ts_iso))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if ts > cutoff:
                out.append(ts.isoformat())
        except (ValueError, TypeError):
            continue
    return out


def _write_log(entries: list[str]) -> None:
    db.runtime_set(_LOG_KEY, json.dumps(entries))


def proactive_count_last_7d() -> int:
    return len(_read_log())


def record_proactive_sent() -> int:
    """Append now() to the rolling log and persist. Returns new count."""
    log = _read_log()
    log.append(datetime.now(UTC).isoformat())
    _write_log(log)
    return len(log)


def can_send_proactive(source: str | None) -> tuple[bool, str]:
    """Decide whether a candidate proactive may go out.

    Returns ``(allowed, reason)``. ``reason`` is a one-line explanation for logs.
    """
    if not _governor_enabled():
        return True, "governor_disabled"
    if proactive_count_last_7d() >= _max_per_7d():
        return False, f"cap_reached ({_max_per_7d()}/7d)"
    allowed = _allowed_sources()
    if allowed and (source is None or source not in allowed):
        return False, f"source_not_justified ({source!r} not in allowed)"
    return True, "ok"


# ---------- soft-scarcity beat ----------

def _scarcity_enabled() -> bool:
    return bool(cfg.get("soft_scarcity.enabled", False))


def _skip_probability() -> float:
    return float(cfg.get("soft_scarcity.skip_probability_per_eligible_window", 0.10))


def _min_days_between_skips() -> int:
    return int(cfg.get("soft_scarcity.min_days_between_skips", 5))


def _scarcity_skip_duration_hours() -> float:
    """How long a single skip-window lasts. Default 4h — long enough that the
    user notices the absence; short enough that we don't drop a real proactive
    they were waiting for."""
    return float(cfg.get("soft_scarcity.skip_window_hours", 4))


def _read_iso(key: str) -> datetime | None:
    raw = db.runtime_get(key)
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return ts
    except (ValueError, TypeError):
        return None


def in_scarcity_skip_window() -> bool:
    """If true, heartbeat windows should silently skip."""
    if not _scarcity_enabled():
        return False
    until = _read_iso(_SKIP_UNTIL_KEY)
    return until is not None and datetime.now(UTC) < until


def maybe_open_scarcity_skip() -> bool:
    """Probabilistic gate: when called on an eligible window, sometimes open a
    skip period. Respects the min-days-between-skips cooldown.

    Returns True if a new skip window was opened.
    """
    if not _scarcity_enabled():
        return False
    if in_scarcity_skip_window():
        return False
    last_skip = _read_iso(_LAST_SKIP_KEY)
    if last_skip is not None:
        days_since = (datetime.now(UTC) - last_skip).total_seconds() / 86400
        if days_since < _min_days_between_skips():
            return False
    if random.random() > _skip_probability():
        return False
    now = datetime.now(UTC)
    until = now + timedelta(hours=_scarcity_skip_duration_hours())
    db.runtime_set(_SKIP_UNTIL_KEY, until.isoformat())
    db.runtime_set(_LAST_SKIP_KEY, now.isoformat())
    db.append_thought(
        f"scarcity beat: going quiet until {until.isoformat()}. "
        "no explanation when i'm back."
    )
    logger.info("scarcity skip window opened until %s", until.isoformat())
    return True
