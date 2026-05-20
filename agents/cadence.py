"""Proactive cadence governor.

State in runtime_state:

  - ``proactive_log_v1`` — JSON list of ISO timestamps of sent proactive
    messages over the rolling 7-day window. Pruned on every read.

The cadence governor caps total proactives per 7d AND requires each candidate
to declare a justified source (``open_loop`` / ``pattern_observation`` /
``lexicon_callback`` / ``noticed_change`` / ``recent_episode_callback`` /
``calendar_event`` / ``reengage_silence``). Without a source, the heartbeat is
vetoed even if under the cap.

Caps and source lists come from ``config/engagement.yaml -> cadence_governor``.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from storage import db

from . import config as cfg

logger = logging.getLogger(__name__)

_LOG_KEY = "proactive_log_v1"


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
