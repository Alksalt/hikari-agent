"""Proactive cadence governor — 3-pool architecture.

Three pools, each with its own rolling-7d counter, cap, and allowed_sources:

  - ``user_anchored``      — triggered by user context (callbacks, reactions)
  - ``agent_spontaneous``  — Hikari-initiated proactives (heartbeat, reengage, calendar)
  - ``scheduled_ceremony`` — scheduled routines (daily_checkin, morning_brief, etc.)

State in runtime_state:
  - ``proactive_user_anchored_log_v1``  — user_anchored pool log
  - ``proactive_log_v1``                — agent_spontaneous pool log (KEEP key for compat)
  - ``proactive_ceremony_log_v1``       — scheduled_ceremony pool log

Caps and source lists come from
``config/engagement.yaml -> cadence_governor.pools.<pool>``.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from storage import db

from . import config as cfg

logger = logging.getLogger(__name__)


class Pool(StrEnum):
    USER_ANCHORED = "user_anchored"
    AGENT_SPONTANEOUS = "agent_spontaneous"
    SCHEDULED_CEREMONY = "scheduled_ceremony"


_LOG_KEYS: dict[Pool, str] = {
    Pool.USER_ANCHORED:      "proactive_user_anchored_log_v1",
    Pool.AGENT_SPONTANEOUS:  "proactive_log_v1",          # KEEP existing key for backward compat
    Pool.SCHEDULED_CEREMONY: "proactive_ceremony_log_v1",
}


# ---------- internal helpers ----------

def _governor_enabled() -> bool:
    return bool(cfg.get("cadence_governor.enabled", True))


def _pool_config(pool: Pool) -> dict:
    """Return the config dict for a pool, or {} if not configured."""
    raw = cfg.get(f"cadence_governor.pools.{pool.value}") or {}
    return raw if isinstance(raw, dict) else {}


def _max_per_7d(pool: Pool) -> int:
    return int(_pool_config(pool).get("max_per_7d", 4))


def _allowed_sources_for_pool(pool: Pool) -> set[str]:
    raw = _pool_config(pool).get("allowed_sources") or []
    return set(raw)


def _resolve_pool(source: str) -> Pool | None:
    """Look up which pool claims this source. Returns None if unrecognised."""
    for pool in Pool:
        if source in _allowed_sources_for_pool(pool):
            return pool
    return None


def _read_log(pool: Pool) -> list[str]:
    key = _LOG_KEYS[pool]
    raw = db.runtime_get(key) or ""
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


def _write_log(pool: Pool, entries: list[str]) -> None:
    db.runtime_set(_LOG_KEYS[pool], json.dumps(entries))


def _count_last_7d(pool: Pool) -> int:
    """Return the number of sends in this pool in the rolling 7d window."""
    return len(_read_log(pool))


def _append_now(pool: Pool) -> int:
    """Append current time to pool's log, persist. Returns new count."""
    log = _read_log(pool)
    log.append(datetime.now(UTC).isoformat())
    _write_log(pool, log)
    return len(log)


# ---------- public API ----------

def can_send(source: str, pool: Pool | None = None) -> tuple[bool, str]:
    """Decide whether a candidate proactive may go out.

    If ``pool`` is None, resolves via ``_resolve_pool(source)``.
    Returns ``(allowed, reason)`` — reason is a one-line string for logs.
    """
    if not _governor_enabled():
        return True, "governor_disabled"

    if pool is None:
        pool = _resolve_pool(source)
        if pool is None:
            return False, f"source_not_justified ({source!r})"

    # Check the source is in the declared pool.
    allowed_srcs = _allowed_sources_for_pool(pool)
    if allowed_srcs and source not in allowed_srcs:
        return False, f"source_not_in_pool ({source!r} not in {pool.value})"

    cap = _max_per_7d(pool)
    count = _count_last_7d(pool)
    if count >= cap:
        return False, f"cap_reached ({cap}/7d, pool={pool.value})"

    return True, "ok"


def record_spontaneous_sent(source: str) -> int:
    """Record a send in the agent_spontaneous pool. Returns new 7d count."""
    logger.debug("cadence: record_spontaneous_sent(source=%r)", source)
    return _append_now(Pool.AGENT_SPONTANEOUS)


def record_ceremony_sent(source: str) -> int:
    """Record a send in the scheduled_ceremony pool. Returns new 7d count."""
    logger.debug("cadence: record_ceremony_sent(source=%r)", source)
    return _append_now(Pool.SCHEDULED_CEREMONY)


def record_user_anchored_sent(source: str) -> int:
    """Record a send in the user_anchored pool. Returns new 7d count."""
    logger.debug("cadence: record_user_anchored_sent(source=%r)", source)
    return _append_now(Pool.USER_ANCHORED)


# ---------- eval / test harness ----------

class _SimulationState:
    """Ephemeral state used by simulate_emission. Not persisted to DB."""

    def __init__(self) -> None:
        self._logs: dict[Pool, list[datetime]] = {p: [] for p in Pool}
        self._now: datetime | None = None  # simulated clock — set to latest candidate_at

    def count(self, pool: Pool) -> int:
        if self._now is None:
            return 0
        cutoff = self._now - timedelta(days=7)
        return sum(1 for ts in self._logs[pool] if ts > cutoff)

    def record(self, pool: Pool, at: datetime) -> None:
        self._logs[pool].append(at)


_SIM_STATE: _SimulationState | None = None


def _get_sim_state() -> _SimulationState:
    global _SIM_STATE
    if _SIM_STATE is None:
        _SIM_STATE = _SimulationState()
    return _SIM_STATE


def reset_simulation() -> None:
    """Reset ephemeral simulation state. Call before each cadence eval run."""
    global _SIM_STATE
    _SIM_STATE = _SimulationState()


def simulate_emission(source: str, candidate_at: str) -> bool:
    """Thin eval harness — decide whether a candidate emits, updating ephemeral state.

    Uses the same pool caps from config as the live path, but against in-memory
    counters so tests remain deterministic and never touch the DB.

    Returns True if the candidate would emit, False if suppressed by cap.
    """
    pool = _resolve_pool(source)
    if pool is None:
        logger.debug("simulate_emission: source %r not in any pool — suppress", source)
        return False

    state = _get_sim_state()
    at = datetime.fromisoformat(candidate_at)
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    if state._now is None or at > state._now:
        state._now = at
    cap = _max_per_7d(pool)
    count = state.count(pool)
    if count >= cap:
        logger.debug(
            "simulate_emission: cap_reached (%d/%d 7d, pool=%s) for source=%r",
            count,
            cap,
            pool.value,
            source,
        )
        return False

    state.record(pool, at)
    return True

