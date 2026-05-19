"""Cost + tool-call budget tracking. **READ-ONLY by design** — Hikari is never
refused a turn based on these numbers. The /cost command reads them; that's it.

Two counters:
  - Per-conversation tool-call window: 30 tool calls per 5-minute rolling window.
    Stored in runtime_state as "budget_window_<chat_id>" → JSON list of timestamps.
  - Per-day USD cap: env HIKARI_DAILY_CAP_USD (default $5). Sum of cost_today
    (incrementing counter in runtime_state) + sum(background_tasks.cost_usd today).

The cap is a readout, not a refusal gate. `daily_cap_exceeded()` exists for
future enforcement but is currently not called anywhere. Per-turn billing is
already capped via SDK `max_budget_usd` on individual ClaudeAgentOptions.

FUTURE WORK (deferred per user instruction 2026-05-19): if/when we want soft
warnings (not hard refusals) on cap-exceeded, the right hook is in
dispatch_claude_session — log a "you're past cap" line to the chat but still run.
Never wire daily_cap_exceeded() into a return-early path on any tool.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime

from agents import config as cfg
from storage import db

logger = logging.getLogger(__name__)


def _call_window_sec() -> int:
    return int(cfg.get("budget.call_window_sec", 300))


def _call_window_max() -> int:
    return int(cfg.get("budget.call_window_max", 30))


def _default_daily_cap() -> float:
    env_key = cfg.get("budget.daily_cap_usd_env", "HIKARI_DAILY_CAP_USD")
    default = float(cfg.get("budget.daily_cap_usd_default", 5.0))
    return cfg.env_float(env_key, default)


def record_tool_call(chat_id: int) -> tuple[bool, int]:
    """Append the current timestamp to the window. Returns (within_budget, count_in_window).
    Drops timestamps older than ``budget.call_window_sec``."""
    key = f"budget_window_{chat_id}"
    now = time.time()
    window = _call_window_sec()
    max_calls = _call_window_max()
    raw = db.runtime_get(key)
    try:
        timestamps = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        timestamps = []
    timestamps = [t for t in timestamps if (now - float(t)) < window]
    timestamps.append(now)
    db.runtime_set(key, json.dumps(timestamps))
    return (len(timestamps) <= max_calls, len(timestamps))


def calls_in_window(chat_id: int) -> int:
    key = f"budget_window_{chat_id}"
    raw = db.runtime_get(key)
    if not raw:
        return 0
    try:
        timestamps = json.loads(raw)
    except json.JSONDecodeError:
        return 0
    now = time.time()
    window = _call_window_sec()
    return sum(1 for t in timestamps if (now - float(t)) < window)


def record_cost(usd: float) -> float:
    """Add to today's running cost. Returns new total."""
    today_iso = datetime.now(UTC).date().isoformat()
    last_date = db.runtime_get("cost_today_date")
    if last_date != today_iso:
        # New day — reset.
        db.runtime_set("cost_today", "0.0")
        db.runtime_set("cost_today_date", today_iso)
    current = float(db.runtime_get("cost_today") or 0.0)
    new_total = current + float(usd)
    db.runtime_set("cost_today", f"{new_total:.6f}")
    return new_total


def cost_today() -> float:
    today_iso = datetime.now(UTC).date().isoformat()
    last_date = db.runtime_get("cost_today_date")
    if last_date != today_iso:
        return 0.0
    return float(db.runtime_get("cost_today") or 0.0)


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
