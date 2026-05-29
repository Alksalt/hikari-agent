"""Cost budget tracking. **READ-ONLY by design** — Hikari is never refused a
turn based on these numbers. The /cost command reads them; that's it.

Counter: per-day USD cap — env HIKARI_DAILY_CAP_USD (default $5). Tracked via
cost_today (runtime_state key) + sum(background_tasks.cost_usd today).

The cap is a readout, not a refusal gate. `daily_cap_exceeded()` exists for
future enforcement but is currently not called anywhere. Per-turn billing is
already capped via SDK `max_budget_usd` on individual ClaudeAgentOptions.

FUTURE WORK (deferred per user instruction 2026-05-19): if/when we want soft
warnings (not hard refusals) on cap-exceeded, the right hook is in
dispatch_claude_session — log a "you're past cap" line to the chat but still run.
Never wire daily_cap_exceeded() into a return-early path on any tool.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from agents import config as cfg
from storage import db

logger = logging.getLogger(__name__)


def _default_daily_cap() -> float:
    env_key = cfg.get("budget.daily_cap_usd_env", "HIKARI_DAILY_CAP_USD")
    default = float(cfg.get("budget.daily_cap_usd_default", 5.0))
    return cfg.env_float(env_key, default)


def cost_today() -> float:
    midnight_iso = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    with db._conn() as c:
        row = c.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS s FROM llm_costs WHERE ts >= ?",
            (midnight_iso,),
        ).fetchone()
    return float(row["s"] or 0.0)


def background_cost_today() -> float:
    today_iso = datetime.now(UTC).date().isoformat()
    with db._conn() as c:
        row = c.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS s FROM background_tasks "
            "WHERE substr(started_at, 1, 10) = ?",
            (today_iso,),
        ).fetchone()
    return float(row["s"] or 0.0)


def daily_cap() -> float:
    return _default_daily_cap()


def daily_cap_remaining() -> float:
    return daily_cap() - cost_today() - background_cost_today()


def daily_cap_exceeded() -> bool:
    return daily_cap_remaining() <= 0.0
