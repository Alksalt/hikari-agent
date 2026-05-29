"""Telegram cockpit — pure formatting helpers for operator commands.

All public functions return plain strings ≤3900 chars (enforced via
_truncate_3900).  No telegram objects; no DB writes except settings.set.
This separation makes every formatter unit-testable without telegram mocks.

Wave 3 — inline keyboards, /diary, /links, /receipt, /decision, /voice,
          /reminders pagination, /proactive simplification, /tools rewrite.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime

_OAUTH_PROBE_CACHE: dict[str, tuple[str, float]] = {}
_OAUTH_PROBE_TTL_SEC = 60.0

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Command registry — source of truth for /help AND /tools list
# ---------------------------------------------------------------------------

# Ordered by usage tier so the Telegram autocomplete menu surfaces
# daily-use commands first (dict insertion order = menu order via
# set_my_commands in telegram_bridge.py). Cuts: `/start` (redundant
# with texting), `/grab_stickers` (one-time setup), `/memory_diff`
# (dev-only) — handlers stay registered, just hidden from the menu.
_COMMANDS: dict[str, str] = {
    # Tier 1 — daily
    "silence":      "mute proactives for N minutes (default 120)",
    "unsilence":    "cancel active silence window",
    "checkin":      "morning checkin: run now / skip tomorrow",
    "memory":       "query memory — /memory [search] | fact <id> | forget <id> | correct <id> <new>",
    # Tier 2 — weekly / when needed
    "reminders":    "list active reminders with snooze/dismiss buttons (paginated)",
    "status":       "system status: uptime, scheduler, MCP, DB, cost, OAuth",
    "proactive":    "manage proactive sources, see recent sends, snooze a source",
    "tasks":        "list open background tasks",
    "cancel":       "cancel a running background task by id",
    "help":         "show this command list",
    # Tier 3 — new in Wave 3
    "diary":        "last 5 diary entries paginated",
    "links":        "search bookmark shelf; no arg = all recent",
    "receipt":      "day/week receipt with category filter buttons",
}

# ---------------------------------------------------------------------------
# Settings allowlist
# ---------------------------------------------------------------------------

def _read_silence_default_minutes() -> str:
    from storage import db as _db
    override = _db.runtime_get("settings.silence.default_minutes")
    if override is not None:
        return override
    from agents import config as _cfg
    return str(_cfg.get("silence.default_minutes", 120))


def _write_silence_default_minutes(value: str) -> None:
    n = int(value)
    if n <= 0:
        raise ValueError(f"silence.default_minutes must be > 0, got {value!r}")
    from storage import db as _db
    _db.runtime_set("settings.silence.default_minutes", str(n))


def _read_graphiti_enabled() -> str:
    return os.environ.get("GRAPHITI_ENABLED", "true")


def _write_graphiti_enabled(value: str) -> None:
    from storage import db as _db
    v = value.strip().lower()
    if v not in ("true", "false", "1", "0"):
        raise ValueError(f"GRAPHITI_ENABLED must be true/false, got {value!r}")
    _db.runtime_set("settings.GRAPHITI_ENABLED", v)
    os.environ["GRAPHITI_ENABLED"] = v
    # Hot-toggle the live scheduler job without restart.
    try:
        from agents import telegram_bridge as _bridge
        scheduler = _bridge._live_scheduler()
        if scheduler is not None:
            from agents.scheduler import _add_graph_outbox_drain_job
            enabled = v not in ("false", "0")
            if enabled:
                _add_graph_outbox_drain_job(scheduler)
                try:
                    scheduler.resume_job("graph_outbox_drain")
                except Exception:
                    pass
            else:
                try:
                    scheduler.pause_job("graph_outbox_drain")
                except Exception:
                    pass
    except Exception:
        logger.exception("_write_graphiti_enabled: scheduler hot-toggle failed (non-fatal)")


def _read_auth_precheck() -> str:
    from agents.auth_precheck import resolve_mode
    return resolve_mode()


def _write_auth_precheck(value: str) -> None:
    v = value.strip().lower()
    if v not in ("enforce", "shadow", "off"):
        raise ValueError(f"AUTH_PRECHECK must be enforce/shadow/off, got {value!r}")
    os.environ["AUTH_PRECHECK"] = v
    os.environ["AUTH_PRECHECK_OVERRIDE"] = v


def _read_proactive_enabled() -> str:
    from storage import db as _db
    raw = _db.runtime_get("proactive_enabled_sources_override")
    if raw is None:
        from agents import config as _cfg
        sources = _cfg.get("proactive.default_enabled_sources")
        return str(sorted(sources) if sources else [])
    return raw


def _write_proactive_enabled(value: str) -> None:
    # Accepts "true"/"false" (global toggle) or JSON list
    from storage import db as _db
    v = value.strip()
    if v.lower() in ("true", "false"):
        # global on/off: load current list, or reset to default
        if v.lower() == "true":
            _db.runtime_set("proactive_enabled_sources_override", None)
        else:
            _db.runtime_set("proactive_enabled_sources_override", "[]")
    else:
        # treat as JSON list
        sources = json.loads(v)
        _db.runtime_set("proactive_enabled_sources_override", json.dumps(sorted(sources)))


def _patch_yaml_key(dotted_key: str, value: object) -> None:
    """Write a value at a dotted key path in engagement.yaml and reload config."""
    import os
    import tempfile
    import yaml
    from agents.config import _config_path
    path = _config_path()
    with open(path) as _f:
        data = yaml.safe_load(_f) or {}
    parts = dotted_key.split(".")
    node = data
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value
    tmp_fd, tmp_path = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(tmp_fd, "w") as _f:
            yaml.dump(data, _f, default_flow_style=False, allow_unicode=True)
            _f.flush()
            os.fsync(_f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    from agents import config as _cfg
    _cfg.reload()


def _read_quiet_start_hour() -> str:
    from agents import config as _cfg
    return str(_cfg.get("proactive.quiet_start_hour", 23))


def _write_quiet_start_hour(value: str) -> None:
    n = int(value)
    if not (0 <= n <= 23):
        raise ValueError(f"quiet_start_hour must be 0-23, got {n}")
    _patch_yaml_key("proactive.quiet_start_hour", n)


def _read_quiet_end_hour() -> str:
    from agents import config as _cfg
    return str(_cfg.get("proactive.quiet_end_hour", 8))


def _write_quiet_end_hour(value: str) -> None:
    n = int(value)
    if not (0 <= n <= 23):
        raise ValueError(f"quiet_end_hour must be 0-23, got {n}")
    _patch_yaml_key("proactive.quiet_end_hour", n)


def _read_aux_model_provider() -> str:
    from agents import config as _cfg
    return str(_cfg.get("aux_model.provider", "openrouter"))


def _write_aux_model_provider(value: str) -> None:
    v = value.strip().lower()
    if v != "openrouter":
        raise ValueError(f"aux_model.provider must be openrouter, got {v!r}")
    _patch_yaml_key("aux_model.provider", v)


def _read_aux_model_name() -> str:
    from agents import config as _cfg
    return str(_cfg.get("aux_model.model", "deepseek/deepseek-v4-flash"))


def _write_aux_model_name(value: str) -> None:
    v = value.strip()
    if not v:
        raise ValueError("aux_model.model cannot be empty")
    _patch_yaml_key("aux_model.model", v)


def _read_scheduler_gate_enabled() -> str:
    from agents import config as _cfg
    return str(bool(_cfg.get("proactive.scheduler_gate_enabled", True))).lower()


def _write_scheduler_gate_enabled(value: str) -> None:
    v = value.strip().lower()
    if v not in ("true", "false", "1", "0"):
        raise ValueError(f"scheduler_gate_enabled must be true/false, got {v!r}")
    enabled = v not in ("false", "0")
    _patch_yaml_key("proactive.scheduler_gate_enabled", enabled)


_SETTINGS_ALLOWLIST: dict[str, dict] = {
    "silence.default_minutes": {
        "type": "int",
        "validate": lambda v: int(v) > 0,
        "reader": _read_silence_default_minutes,
        "writer": _write_silence_default_minutes,
        "doc": "default silence duration in minutes when /silence is called with no arg",
    },
    "GRAPHITI_ENABLED": {
        "type": "bool",
        "validate": lambda v: v.strip().lower() in ("true", "false", "1", "0"),
        "reader": _read_graphiti_enabled,
        "writer": _write_graphiti_enabled,
        "doc": "enable/disable Graphiti knowledge-graph outbox drain (true/false)",
    },
    "AUTH_PRECHECK": {
        "type": "enum[enforce,shadow,off]",
        "validate": lambda v: v.strip().lower() in ("enforce", "shadow", "off"),
        "reader": _read_auth_precheck,
        "writer": _write_auth_precheck,
        "doc": "auth pre-check mode: enforce (block), shadow (log only), off",
    },
    "proactive.enabled": {
        "type": "bool|json_list",
        "validate": lambda v: True,  # writer validates
        "reader": _read_proactive_enabled,
        "writer": _write_proactive_enabled,
        "doc": "true/false (global toggle) or JSON list of enabled source ids",
    },
    "quiet_start_hour": {
        "type": "int[0-23]",
        "validate": lambda v: v.strip().isdigit() and 0 <= int(v.strip()) <= 23,
        "reader": _read_quiet_start_hour,
        "writer": _write_quiet_start_hour,
        "doc": "hour (0-23, local time via HOME_TZ) when quiet hours begin — no proactive messages",
    },
    "quiet_end_hour": {
        "type": "int[0-23]",
        "validate": lambda v: v.strip().isdigit() and 0 <= int(v.strip()) <= 23,
        "reader": _read_quiet_end_hour,
        "writer": _write_quiet_end_hour,
        "doc": "hour (0-23, local time via HOME_TZ) when quiet hours end — proactives resume",
    },
    "aux_model.provider": {
        "type": "enum[openrouter]",
        "validate": lambda v: v.strip().lower() == "openrouter",
        "reader": _read_aux_model_provider,
        "writer": _write_aux_model_provider,
        "doc": "LLM provider for cheap aux ops (reflection, task extraction)",
    },
    "aux_model.model": {
        "type": "str",
        "validate": lambda v: bool(v.strip()),
        "reader": _read_aux_model_name,
        "writer": _write_aux_model_name,
        "doc": "model ID for aux_model.provider=openrouter (e.g. deepseek/deepseek-v4-flash)",
    },
    "proactive.scheduler_gate_enabled": {
        "type": "bool",
        "validate": lambda v: v.strip().lower() in ("true", "false", "1", "0"),
        "reader": _read_scheduler_gate_enabled,
        "writer": _write_scheduler_gate_enabled,
        "doc": "enable/disable the wakeAgent quiet-hours gate for scheduled engagement ticks",
    },
}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _truncate_3900(text: str) -> str:
    """Single chokepoint — all public formatters pipe through here."""
    limit = 3900
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…[truncated]"


def _uptime_str() -> str:
    try:
        from agents.telegram_bridge import _BOOT_TIME  # type: ignore[attr-defined]
        elapsed = time.time() - _BOOT_TIME
    except (ImportError, AttributeError):
        return "unknown"
    h = int(elapsed // 3600)
    m = int((elapsed % 3600) // 60)
    s = int(elapsed % 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _silence_state() -> tuple[bool, str]:
    """Returns (is_silenced, human_readable_until_or_empty)."""
    from storage import db as _db
    raw = _db.runtime_get("silence_until")
    if not raw:
        return False, ""
    try:
        until = datetime.fromisoformat(raw)
        if until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        if datetime.now(UTC) >= until:
            return False, ""
        return True, until.strftime("%H:%M UTC")
    except (ValueError, TypeError):
        return False, ""


async def _probe_google_cached() -> str:
    now = time.time()
    cached = _OAUTH_PROBE_CACHE.get("google")
    if cached and cached[1] > now:
        return cached[0]
    try:
        from agents.google_health import probe_google_token
        healthy, reason = await probe_google_token(timeout_sec=10.0)
        state = "ok" if healthy else (reason or "unknown")
    except Exception as exc:
        state = f"probe_error:{type(exc).__name__}"
    _OAUTH_PROBE_CACHE["google"] = (state, now + _OAUTH_PROBE_TTL_SEC)
    return state


async def _oauth_states() -> dict[str, str]:
    return {"google": await _probe_google_cached()}


def _parse_duration(s: str) -> int | None:
    """Parse '30m', '2h', '1d' → seconds.  Returns None on bad input."""
    s = s.strip().lower()
    if not s:
        return None
    if s.endswith("d"):
        try:
            return int(s[:-1]) * 86400
        except ValueError:
            return None
    if s.endswith("h"):
        try:
            return int(s[:-1]) * 3600
        except ValueError:
            return None
    if s.endswith("m"):
        try:
            return int(s[:-1]) * 60
        except ValueError:
            return None
    # bare integer → assume minutes
    try:
        return int(s) * 60
    except ValueError:
        return None

# ---------------------------------------------------------------------------
# Public formatters
# ---------------------------------------------------------------------------

def format_help() -> str:
    lines = ["commands:"]
    for cmd, desc in _COMMANDS.items():
        lines.append(f"  /{cmd} — {desc}")
    return _truncate_3900("\n".join(lines))


async def format_status(app) -> str:
    from storage import db as _db

    lines: list[str] = []

    # uptime
    lines.append(f"uptime: {_uptime_str()}")

    # silence
    silenced, until_str = _silence_state()
    if silenced:
        lines.append(f"silence: on until {until_str}")
    else:
        lines.append("silence: off")

    # scheduler jobs
    try:
        scheduler = app.bot_data.get("scheduler") if app.bot_data else None
        if scheduler is not None:
            jobs = scheduler.get_jobs()
            job_ids = [j.id for j in jobs]
            lines.append(f"scheduler: {len(jobs)} job(s) — {', '.join(job_ids)}")
        else:
            lines.append("scheduler: not started")
    except Exception as exc:
        lines.append(f"scheduler: error ({exc})")

    # MCP warm pool
    try:
        from agents.mcp_manager import MANAGER as _mcp_mgr
        warm = _mcp_mgr.warm_servers()
        lines.append(f"mcp warm: {sorted(warm) if warm else 'none'}")
    except Exception as exc:
        lines.append(f"mcp: error ({exc})")

    # OAuth
    for provider, state in (await _oauth_states()).items():
        lines.append(f"oauth.{provider}: {state}")

    # DB row counts — use db.status_counts() instead of raw SQL
    try:
        counts = _db.status_counts()
        facts_counts = counts.get("facts", {})
        n_facts = facts_counts.get("active", 0) + facts_counts.get("pinned", 0)
        n_msgs = 0
        n_eps = 0
        n_tasks = 0
        n_pending_approvals = 0
        with _db._conn() as c:
            n_msgs = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            n_eps = c.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
            tasks_counts = counts.get("work_packets", {})
            n_tasks_raw = c.execute(
                "SELECT COUNT(*) FROM tasks WHERE status='open'"
            ).fetchone()[0]
            n_pending_approvals = c.execute(
                "SELECT COUNT(*) FROM approvals "
                "WHERE status='pending' AND gate_kind='gatekeeper'"
            ).fetchone()[0]
        n_tasks = n_tasks_raw
        lines.append(
            f"db: {n_facts} facts, {n_msgs} msgs, {n_eps} episodes, "
            f"{n_tasks} open tasks"
        )
        lines.append(f"pending approvals: {n_pending_approvals}")
        # surfaced from status_counts: reminders active count
        rem_counts = counts.get("reminders", {})
        n_rem_active = rem_counts.get("active", 0)
        if n_rem_active:
            lines.append(f"active reminders: {n_rem_active}")
    except Exception as exc:
        lines.append(f"db: error ({exc})")

    # cost today
    try:
        chat_today = float(_db.runtime_get("cost_today") or 0.0)
        today_iso = datetime.now(UTC).date().isoformat()
        with _db._conn() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS bg FROM background_tasks "
                "WHERE substr(started_at, 1, 10) = ?",
                (today_iso,),
            ).fetchone()
        bg_cost = float(row["bg"] or 0.0)
        from tools import budget as _budget
        cap = _budget.daily_cap()
        lines.append(
            f"cost today: ~${chat_today + bg_cost:.3f} "
            f"(chat ${chat_today:.3f} + dispatch ${bg_cost:.3f}) / cap ${cap:.2f}"
        )
    except Exception as exc:
        lines.append(f"cost: error ({exc})")

    # main-chat cost rollup (llm_costs table — Phase C)
    try:
        from storage import db as _db_mod
        from agents import config as _cfg
        rollup_24h = _db_mod.llm_costs_rollup(window_hours=24)
        rollup_30d = _db_mod.llm_costs_rollup(window_hours=30 * 24)
        monthly_credit = float(
            _cfg.get("runtime.agent_sdk_monthly_credit_usd", 200)
        )
        alert_threshold = monthly_credit * 0.80
        alert_str = (
            f"  ⚠ 80% of ${monthly_credit:.0f} credit"
            if rollup_30d["total_cost_usd"] > alert_threshold
            else ""
        )
        lines.append(
            f"chat cost   24h: ${rollup_24h['total_cost_usd']:.2f}"
            f" ({rollup_24h['n_rows']} turns)"
        )
        lines.append(
            f"            30d: ${rollup_30d['total_cost_usd']:.2f}{alert_str}"
        )
        if rollup_30d["by_model"]:
            top_models = list(rollup_30d["by_model"].items())[:3]
            model_str = " · ".join(
                f"{m} ${c:.2f}" for m, c in top_models
            )
            lines.append(f"            top models: {model_str}")
    except Exception as exc:
        lines.append(f"chat cost: error ({exc})")

    # proactive 7d count
    try:
        rows_7d = _db.proactive_events_recent(days=7)
        sent_7d = sum(1 for r in rows_7d if r.get("status") == "sent")
        lines.append(f"proactive 7d sends: {sent_7d}")
    except Exception as exc:
        lines.append(f"proactive: error ({exc})")

    # graph_outbox pending + failed
    try:
        pending_graph = len(_db.graph_outbox_pending(limit=500))
        failed_stats = _db.graph_outbox_failed_stats()
        failed_graph = failed_stats.get("count", 0)
        last_err = failed_stats.get("last_error")
        err_note = f" last_error={last_err!r}" if last_err else ""
        lines.append(f"graph outbox pending: {pending_graph}  failed: {failed_graph}{err_note}")
    except Exception as exc:
        lines.append(f"graph outbox: error ({exc})")

    # media_events totals
    try:
        media_counts = _db.media_events_counts()
        if media_counts:
            mc_str = "  ".join(f"{k}:{v}" for k, v in sorted(media_counts.items()))
            lines.append(f"media events: {mc_str}")
        else:
            lines.append("media events: none recorded")
    except Exception as exc:
        lines.append(f"media events: error ({exc})")

    # sticker pool health — show raw vs valid so a botched yaml edit
    # (malformed dict entry, wrong type) doesn't hide behind the raw count.
    try:
        from agents import stickers as _stickers
        counts = _stickers.pool_counts()
        raw, valid = counts["raw"], counts["valid"]
        if raw == 0:
            lines.append("stickers: degraded (pool empty — /status shows no file_ids)")
        elif valid < raw:
            lines.append(f"stickers: degraded ({valid}/{raw} valid — see logs for malformed entries)")
        else:
            lines.append(f"stickers: {valid} in pool")
    except Exception as exc:
        lines.append(f"stickers: error ({exc})")

    return _truncate_3900("\n".join(lines))


def format_tools(subcmd: str, args: list[str]) -> str:
    """Per-family counts + last-call time + warm-pool health.

    Subcommands:
      (no arg)  — per-family summary with last-call timestamp + warm-pool
      recent    — last 20 tool calls
      audit     — last 20 audit-log rows
      policy    — full registry grouped by access_mode (legacy view)
    """
    subcmd = (subcmd or "summary").lower()

    if subcmd in ("recent", "calls"):
        from storage import db as _db
        rows = _db.tool_calls_recent(20)
        if not rows:
            return "no tool calls recorded yet."
        lines = [f"recent tool calls ({len(rows)}):"]
        for r in rows:
            ts = (r.get("started_at") or "")[:16]
            tool = r.get("tool_id") or "?"
            dur = r.get("duration_ms")
            ok = "ok" if r.get("success") else "err"
            err_cls = r.get("error_class") or ""
            err_note = f" [{err_cls}]" if err_cls else ""
            dur_note = f" {dur}ms" if dur is not None else ""
            lines.append(f"  {ts}  {tool}  {ok}{err_note}{dur_note}")
        return _truncate_3900("\n".join(lines))

    if subcmd == "audit":
        from storage import db as _db
        rows = _db.audit_recent(20)
        if not rows:
            return "no tool calls in audit log yet."
        lines = [f"audit log ({len(rows)}):"]
        for r in rows:
            ts = (r.get("ts") or "")[:16]
            tool = r.get("tool") or "?"
            lines.append(f"  {ts}  {tool}")
        return _truncate_3900("\n".join(lines))

    if subcmd == "policy":
        # legacy view — group by access_mode
        try:
            from tools._tools_yaml import load_registry
            reg = load_registry()
            tools_list = reg.specs()
        except Exception as exc:
            return f"error loading tool registry: {exc}"
        groups: dict[str, list[str]] = {}
        for spec in tools_list:
            mode = spec.access_mode or "unset"
            groups.setdefault(mode, []).append(spec.id)
        lines = ["tool registry by access_mode:"]
        for mode in sorted(groups):
            ids = sorted(groups[mode])
            lines.append(f"\n[{mode}] ({len(ids)} tools)")
            for tid in ids:
                lines.append(f"  {tid}")
        return _truncate_3900("\n".join(lines))

    # default: per-family summary with last-call + warm-pool health
    try:
        from tools.catalog import get_catalog
        catalog = get_catalog()
        entries = catalog.entries
    except Exception as exc:
        return f"error loading tool catalog: {exc}"

    # Group entries by domain (family)
    families: dict[str, list] = {}
    for entry in entries:
        fam = entry.domain or "other"
        families.setdefault(fam, []).append(entry.name)

    # Get last-call timestamps from audit 7d counts
    last_call_by_tool: dict[str, str] = {}
    try:
        from storage import db as _db
        counts_7d = _db.audit_tool_counts_7d()
        for r in counts_7d:
            last_call_by_tool[r["tool"]] = (r.get("last_ts") or "")[:16]
    except Exception:
        pass

    # Warm-pool health
    warm_servers: set[str] = set()
    try:
        from agents.mcp_manager import MANAGER as _mcp_mgr
        warm_servers = set(_mcp_mgr.warm_servers())
    except Exception:
        pass

    lines = [f"tools by family ({len(families)} families, {len(entries)} total):"]
    for fam in sorted(families):
        fam_tools = sorted(families[fam])
        # Find most recent call across tools in this family
        last_calls = [last_call_by_tool[t] for t in fam_tools if t in last_call_by_tool]
        last_str = max(last_calls) if last_calls else "never"
        lines.append(f"  {fam}: {len(fam_tools)} tools  last={last_str}")

    # Warm pool summary
    if warm_servers:
        lines.append(f"\nwarm pool ({len(warm_servers)}): {', '.join(sorted(warm_servers))}")
    else:
        lines.append("\nwarm pool: none")

    return _truncate_3900("\n".join(lines))


def format_audit(subcmd: str, args: list[str]) -> str:
    from storage import db as _db
    subcmd = (subcmd or "recent").lower()

    def _render_row(r: dict) -> str:
        ts = (r.get("ts") or "")[:16]
        tool = r.get("tool") or "?"
        approved_by = r.get("approved_by") or ""
        summary = (r.get("result_summary") or "")[:60]
        approval_note = f" [approved by {approved_by}]" if approved_by else ""
        return f"  #{r['id']} {ts} {tool}{approval_note}" + (
            f"\n    {summary}" if summary else ""
        )

    if subcmd == "recent":
        try:
            n = int(args[0]) if args else 20
        except (ValueError, IndexError):
            n = 20
        rows = _db.audit_recent(n)
        if not rows:
            return "audit log is empty."
        lines = [f"audit log — last {len(rows)} entries:"]
        for r in rows:
            lines.append(_render_row(r))
        # hash-chain verify line
        if rows:
            latest = rows[0]
            lines.append(
                f"\nlatest hash: {(latest.get('hash_self') or '')[:12]}…"
            )
        return _truncate_3900("\n".join(lines))

    if subcmd == "tools":
        counts = _db.audit_tool_counts_7d()
        if not counts:
            return "no tool activity in the last 7 days."
        lines = ["tool call counts (7d):"]
        for r in counts:
            lines.append(f"  {r['tool']:40s} {r['cnt']:>5}  last {(r.get('last_ts') or '')[:16]}")
        return _truncate_3900("\n".join(lines))

    if subcmd == "approvals":
        rows = _db.audit_approvals_recent(20)
        if not rows:
            return "no approved tool calls in audit log."
        lines = [f"approved tool calls — last {len(rows)}:"]
        for r in rows:
            lines.append(_render_row(r))
        return _truncate_3900("\n".join(lines))

    if subcmd == "id":
        if not args:
            return "usage: /audit id <row_id>"
        try:
            row_id = int(args[0])
        except ValueError:
            return f"invalid id: {args[0]}"
        row = _db.audit_by_id(row_id)
        if row is None:
            return f"audit row #{row_id} not found."
        lines = [
            f"audit #{row['id']}",
            f"  ts:          {row.get('ts') or '?'}",
            f"  tool:        {row.get('tool') or '?'}",
            f"  approved_by: {row.get('approved_by') or '—'}",
            f"  summary:     {row.get('result_summary') or '—'}",
            f"  hash_self:   {(row.get('hash_self') or '')[:24]}…",
            f"  hash_prev:   {(row.get('hash_prev') or '')[:24]}…",
        ]
        args_raw = row.get("args_json_redacted") or ""
        if args_raw:
            lines.append(f"  args:        {args_raw[:200]}")
        try:
            from tools.approvals import _redact as _approvals_redact
            body = _approvals_redact("\n".join(lines))
        except Exception:
            body = "\n".join(lines)
        return _truncate_3900(body)

    if subcmd == "media":
        try:
            rows = _db.media_events_recent(20)
            counts = _db.media_events_counts()
        except Exception as exc:
            return f"media_events: error ({exc})"
        if not rows:
            return "media_events: no records yet."
        total_str = "  ".join(f"{k}:{v}" for k, v in sorted(counts.items()))
        lines = [f"media events — totals: {total_str}", "last 20:"]
        for r in rows:
            ts = (r.get("created_at") or "")[:16]
            kind = r.get("kind") or "?"
            tg_id = r.get("telegram_message_id")
            cap = (r.get("caption") or "")[:40]
            tg_note = f" tg={tg_id}" if tg_id else ""
            cap_note = f" cap={cap!r}" if cap else ""
            lines.append(f"  {ts}  {kind}{tg_note}{cap_note}")
        return _truncate_3900("\n".join(lines))

    return f"unknown audit subcommand: {subcmd!r}. try: recent [N] | tools | approvals | id <id> | media"


def format_settings(subcmd: str, args: list[str]) -> str:
    from storage import db as _db

    # /settings — list all
    if not subcmd or subcmd == "list":
        lines = ["settings:"]
        for key, spec in _SETTINGS_ALLOWLIST.items():
            try:
                val = spec["reader"]()
            except Exception as exc:
                val = f"[error: {exc}]"
            lines.append(f"  {key} = {val}")
            lines.append(f"    {spec['doc']}")
        return _truncate_3900("\n".join(lines))

    if subcmd == "get":
        if not args:
            return "usage: /settings get <key>"
        key = args[0]
        spec = _SETTINGS_ALLOWLIST.get(key)
        if spec is None:
            return f"unknown key: {key!r}. allowed: {list(_SETTINGS_ALLOWLIST)}"
        try:
            val = spec["reader"]()
        except Exception as exc:
            return f"error reading {key}: {exc}"
        return f"{key} = {val}\n{spec['doc']}"

    if subcmd == "set":
        if len(args) < 2:
            return "usage: /settings set <key> <value>"
        key, value = args[0], " ".join(args[1:])
        spec = _SETTINGS_ALLOWLIST.get(key)
        if spec is None:
            return f"unknown key: {key!r}. allowed: {list(_SETTINGS_ALLOWLIST)}"
        try:
            if not spec["validate"](value):
                return f"invalid value {value!r} for {key} (type: {spec['type']})"
            spec["writer"](value)
        except Exception as exc:
            return f"error setting {key}: {exc}"
        # audit trail
        try:
            _db.audit_append(
                "settings.set",
                json.dumps({"key": key, "value": "REDACTED"}),
                f"set {key} (value redacted)",
                approved_by="owner",
            )
        except Exception:
            logger.exception("settings.set: audit_append failed (non-fatal)")
        return f"ok. {key} = {value}"

    return f"unknown subcommand: {subcmd!r}. try: /settings | get <key> | set <key> <value>"


# ---------------------------------------------------------------------------
# /capabilities
# ---------------------------------------------------------------------------

async def format_capabilities() -> str:
    """Return a capabilities table: tool families + MCP server health."""
    import asyncio
    from pathlib import Path

    lines = ["capabilities:"]

    # Tool families — auto-discovered utility modules under tools/
    try:
        from tools._registry import discover_utility_tool_names
        tool_names = list(discover_utility_tool_names())
        tools_root = Path(__file__).parent.parent / "tools"
        # Group by folder name (tool family)
        families: dict[str, int] = {}
        for modinfo_name in sorted(
            p.name for p in tools_root.iterdir()
            if p.is_dir() and not p.name.startswith("_") and (p / "__init__.py").exists()
            and p.name not in {"dispatch", "memory", "wiki", "photos", "codex", "router"}
        ):
            family_tools = [t for t in tool_names if t.startswith(modinfo_name.replace("-", "_"))
                            or t.replace("_", "-").startswith(modinfo_name)]
            families[modinfo_name] = len(family_tools) if family_tools else 1
        # Count skills from the authoritative root (Sprint A flipped to .claude/skills/)
        from tools.skills.core import _SKILLS_ROOT as _active_skills_root
        n_skills = (
            sum(1 for p in _active_skills_root.iterdir()
                if p.is_dir() and (p / "SKILL.md").exists())
            if _active_skills_root.exists()
            else 0
        )
        lines.append(f"\ntool families ({len(families)}):")
        for fam, count in sorted(families.items()):
            lines.append(f"  {fam}: {count} tool(s)")
        lines.append(f"  skills: {n_skills} skill(s) in .claude/skills/")
    except Exception as exc:
        lines.append(f"  [tool discovery failed: {exc}]")

    # MCP servers — probe health with list_tools (timeout 2s each)
    try:
        from tools._tools_yaml import load_registry
        reg = load_registry()
        server_names = sorted(
            s for s in {spec.server for spec in reg._tools}
            if s is not None
        )
    except Exception:
        server_names = ["hikari_memory", "hikari_utility", "hikari_wiki",
                        "hikari_dispatch", "hikari_photo", "google_workspace",
                        "notion", "youtube_transcript"]

    lines.append(f"\nmcp servers ({len(server_names)}):")

    async def _probe(name: str) -> tuple[str, str]:
        try:
            from agents.mcp_manager import MANAGER as _mgr
            warm = _mgr.warm_servers()
            if name in warm:
                return name, "warm"
            return name, "ok"
        except Exception as exc:
            return name, f"err:{type(exc).__name__}"

    try:
        probes = await asyncio.wait_for(
            asyncio.gather(*[_probe(s) for s in server_names]),
            timeout=3.0,
        )
        for server, status in probes:
            lines.append(f"  {server}: {status}")
    except asyncio.TimeoutError:
        lines.append("  [probe timed out]")
    except Exception as exc:
        lines.append(f"  [probe failed: {exc}]")

    return _truncate_3900("\n".join(lines))


def _payload_preview(payload_json: str | None, max_len: int = 50) -> str:
    """Extract a short human-readable preview from payload_json."""
    if not payload_json:
        return ""
    try:
        data = json.loads(payload_json)
        # prefer a 'text' or 'body' or 'content' key
        for key in ("text", "body", "content", "message"):
            if key in data and isinstance(data[key], str):
                return data[key][:max_len]
        # fall back to first string value
        for v in data.values():
            if isinstance(v, str) and v:
                return v[:max_len]
    except (ValueError, TypeError, AttributeError):
        return payload_json[:max_len]
    return ""


def format_proactive_recent(days: int = 7) -> str:
    from storage import db as _db
    rows = _db.proactive_events_recent(days=days, limit=50)
    if not rows:
        return f"no proactive events in the last {days}d."
    lines = [f"proactive sends — last {days}d ({len(rows)} rows):"]
    for r in rows:
        ts = (r.get("sent_at") or "")[:16]
        source = r.get("source") or "?"
        status = r.get("status") or "?"
        up = r.get("thumbs_up") or 0
        dn = r.get("thumbs_down") or 0
        preview = _payload_preview(r.get("payload_json"))
        reaction = f" 👍{up}👎{dn}" if (up or dn) else ""
        lines.append(f"  #{r['id']} {ts} [{source}] {status}{reaction}")
        if preview:
            lines.append(f"    {preview}")
    return _truncate_3900("\n".join(lines))


def format_proactive_why(event_id: int) -> str:
    """Render reason-contract details for a proactive event.

    Renders all known reason-contract columns if present:
    source, anchor, why_now, suggested_action, confidence,
    controls, data_checked, gate decision.
    """
    from storage import db as _db
    row = _db.proactive_event_by_id(event_id)
    if row is None:
        return f"proactive event #{event_id} not found."
    lines = [
        f"proactive event #{row['id']}",
        f"  source:         {row.get('source') or '?'}",
        f"  sent_at:        {row.get('sent_at') or '?'}",
        f"  status:         {row.get('status') or '?'}",
    ]
    aborted = row.get("aborted_reason")
    if aborted:
        lines.append(f"  aborted:        {aborted}")
    # reason-contract columns (may not exist yet in older DBs)
    anchor = row.get("anchor")
    if anchor:
        lines.append(f"  anchor:         {anchor}")
    why_now = row.get("why_now")
    if why_now:
        lines.append(f"  why_now:        {why_now}")
    suggested_action = row.get("suggested_action")
    if suggested_action:
        lines.append(f"  suggested_action: {suggested_action}")
    confidence = row.get("confidence")
    if confidence is not None:
        lines.append(f"  confidence:     {confidence}")
    gate_decision = row.get("gate_decision")
    if gate_decision:
        lines.append(f"  gate_decision:  {gate_decision}")
    data_checked = row.get("data_checked_json") or row.get("data_checked")
    if data_checked:
        lines.append(f"  data_checked:   {data_checked}")
    controls_raw = row.get("controls_json") or row.get("controls")
    if controls_raw:
        lines.append(f"  controls:       {controls_raw}")
    score_novelty = row.get("score_novelty")
    score_actionability = row.get("score_actionability")
    if score_novelty is not None or score_actionability is not None:
        n_str = f"{score_novelty:.2f}" if score_novelty is not None else "?"
        a_str = f"{score_actionability:.2f}" if score_actionability is not None else "?"
        lines.append(f"  score:          novelty={n_str} actionability={a_str}")
    preview = _payload_preview(row.get("payload_json"), max_len=200)
    if preview:
        lines.append(f"  preview:        {preview}")
    up = row.get("thumbs_up") or 0
    dn = row.get("thumbs_down") or 0
    lines.append(f"  feedback:       👍{up} 👎{dn}")
    tg_id = row.get("telegram_message_id")
    if tg_id:
        lines.append(f"  tg_msg_id:      {tg_id}")
    return _truncate_3900("\n".join(lines))


def format_proactive_status() -> str:
    """Simplified /proactive (no args) output:
    - next ping window
    - active sources
    - snoozed sources with TTLs
    """
    from storage import db as _db

    # next ping window from scheduler (best-effort)
    try:
        from agents.engagement.producers import ALL_PRODUCER_IDS, DEFAULT_ENABLED_SOURCES
    except Exception:
        ALL_PRODUCER_IDS = []
        DEFAULT_ENABLED_SOURCES = []

    # enabled sources
    raw_override = _db.runtime_get("proactive_enabled_sources_override")
    try:
        enabled: set[str] = set(json.loads(raw_override)) if raw_override else set(DEFAULT_ENABLED_SOURCES)
    except (ValueError, TypeError):
        enabled = set(DEFAULT_ENABLED_SOURCES)

    # snooze map
    raw_snooze = _db.runtime_get("proactive_snooze_until")
    try:
        snooze_map: dict[str, str] = json.loads(raw_snooze) if raw_snooze else {}
    except (ValueError, TypeError):
        snooze_map = {}

    now_ts = time.time()

    # remove expired snooze entries
    active_snooze: dict[str, str] = {}
    for src, iso in snooze_map.items():
        try:
            until_ts = datetime.fromisoformat(iso).timestamp()
            if until_ts > now_ts:
                active_snooze[src] = iso
        except (ValueError, TypeError):
            pass

    # next ping window: try to read from scheduler or config
    try:
        from agents import config as _cfg
        quiet_start = int(_cfg.get("proactive.quiet_start_hour", 23))
        quiet_end = int(_cfg.get("proactive.quiet_end_hour", 8))
        tz_name = os.environ.get("HOME_TZ") or "UTC"
        from zoneinfo import ZoneInfo
        now_local = datetime.now(ZoneInfo(tz_name))
        h = now_local.hour
        # simple: are we in quiet hours?
        if quiet_start > quiet_end:
            in_quiet = h >= quiet_start or h < quiet_end
        else:
            in_quiet = quiet_start <= h < quiet_end
        if in_quiet:
            ping_window = f"quiet until {quiet_end:02d}:00 {tz_name}"
        else:
            ping_window = f"active (quiet {quiet_start:02d}:00–{quiet_end:02d}:00 {tz_name})"
    except Exception as exc:
        ping_window = f"unknown ({exc})"

    lines = [f"next ping window: {ping_window}"]

    # active sources (enabled and not snoozed)
    active_sources = sorted(s for s in enabled if s not in active_snooze)
    if active_sources:
        lines.append(f"\nactive sources ({len(active_sources)}):")
        for s in active_sources:
            try:
                from storage import db as _db2
                cnt_7d = _db2.proactive_send_count_7d(s)
            except Exception:
                cnt_7d = "?"
            lines.append(f"  {s}  (7d: {cnt_7d})")
    else:
        lines.append("\nactive sources: none")

    # snoozed sources
    if active_snooze:
        lines.append(f"\nsnoozed sources ({len(active_snooze)}):")
        for src, iso in sorted(active_snooze.items()):
            try:
                until_dt = datetime.fromisoformat(iso)
                secs_left = int(until_dt.timestamp() - now_ts)
                if secs_left >= 3600:
                    ttl_str = f"{secs_left // 3600}h {(secs_left % 3600) // 60}m"
                else:
                    ttl_str = f"{secs_left // 60}m"
                lines.append(f"  {src}  (expires in {ttl_str})")
            except (ValueError, TypeError):
                lines.append(f"  {src}  (expires: {iso[:16]})")
    else:
        lines.append("\nsnoozed sources: none")

    return _truncate_3900("\n".join(lines))


def format_proactive_snooze(source: str, duration_str: str) -> str:
    from storage import db as _db
    secs = _parse_duration(duration_str)
    if secs is None:
        return f"can't parse duration {duration_str!r}. use e.g. 30m / 2h / 1d"

    # Read existing snooze map
    raw = _db.runtime_get("proactive_snooze_until")
    try:
        snooze_map: dict[str, str] = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        snooze_map = {}

    until_epoch = time.time() + secs
    until_iso = datetime.fromtimestamp(until_epoch, tz=UTC).isoformat()
    snooze_map[source] = until_iso
    _db.runtime_set("proactive_snooze_until", json.dumps(snooze_map))

    human = duration_str.strip()
    return f"snoozed {source} for {human} (until {until_iso[:16]} UTC)."


# ---------------------------------------------------------------------------
# /silence ack with expiry
# ---------------------------------------------------------------------------

def format_silence_ack(minutes: int) -> str:
    """Return the silence acknowledgment including expiry timestamp in local time."""
    from datetime import timedelta
    until_utc = datetime.now(UTC) + timedelta(minutes=minutes)
    # try local tz
    try:
        from zoneinfo import ZoneInfo
        tz_name = os.environ.get("HOME_TZ") or "UTC"
        until_local = until_utc.astimezone(ZoneInfo(tz_name))
        expiry_str = until_local.strftime(f"%Y-%m-%d %H:%M {tz_name}")
    except Exception:
        expiry_str = until_utc.strftime("%Y-%m-%d %H:%M UTC")
    return f"ok. quiet for {minutes} minutes (until {expiry_str}). don't make me regret it."


# ---------------------------------------------------------------------------
# /memorydump — paginated facts with per-fact inline keyboard data
# ---------------------------------------------------------------------------

_MEMORYDUMP_PAGE_SIZE = 10


def format_memorydump(page: int = 0) -> tuple[str, list[dict]]:
    """Return (text, keyboard_rows) for /memorydump page N.

    keyboard_rows is a list of button rows, each row a list of dicts:
      [{"text": "...", "callback_data": "..."}]

    Bridge-master registers the callbacks; this function only builds the data.
    """
    from storage import db as _db
    all_facts = _db.active_facts(limit=500)
    total = len(all_facts)
    page_size = _MEMORYDUMP_PAGE_SIZE
    start = page * page_size
    page_facts = all_facts[start : start + page_size]

    if not page_facts:
        if page > 0:
            return "no more facts. /memorydump to start over.", []
        return "no active facts in memory.", []

    total_pages = max(1, (total + page_size - 1) // page_size)
    lines = [f"memory dump — page {page + 1}/{total_pages} ({total} facts):"]

    keyboard_rows: list[list[dict]] = []
    for fact in page_facts:
        fid = fact.get("id") or 0
        subj = (fact.get("subject") or "")[:20]
        pred = (fact.get("predicate") or "")[:20]
        obj_ = (fact.get("object") or "")[:30]
        lines.append(f"  #{fid}  {subj} → {pred} → {obj_}")
        keyboard_rows.append([
            {"text": "Forget", "callback_data": f"mem:forget:{fid}"},
            {"text": "Context", "callback_data": f"mem:context:{fid}"},
            {"text": "Pin", "callback_data": f"mem:pin:{fid}"},
        ])

    # pagination row
    nav_row: list[dict] = []
    if page > 0:
        nav_row.append({"text": "< Prev", "callback_data": f"mem:page:{page - 1}"})
    if start + page_size < total:
        nav_row.append({"text": "Next >", "callback_data": f"mem:page:{page + 1}"})
    if nav_row:
        keyboard_rows.append(nav_row)

    return _truncate_3900("\n".join(lines)), keyboard_rows


# ---------------------------------------------------------------------------
# /diary — paginated diary entries
# ---------------------------------------------------------------------------

_DIARY_PAGE_SIZE = 5


def format_diary(page: int = 0) -> tuple[str, list[dict]]:
    """Return (text, nav_keyboard_row) for /diary page N.

    Fetches up to 50 entries from diary_entries; paginates at 5/page.
    keyboard_row is a list of dicts (prev/next buttons).
    """
    from storage import db as _db
    all_entries = _db.diary_entries_recent(limit=50)
    total = len(all_entries)

    if total == 0:
        return "no diary entries yet.", []

    page_size = _DIARY_PAGE_SIZE
    total_pages = max(1, (total + page_size - 1) // page_size)
    start = page * page_size
    page_entries = all_entries[start : start + page_size]

    if not page_entries:
        return f"no entries on page {page + 1} (total pages: {total_pages}).", []

    lines = [f"diary — page {page + 1}/{total_pages}:"]
    for entry in page_entries:
        ts = (entry.get("entry_date") or "")[:10]
        thought = (entry.get("body") or "")[:200]
        lines.append(f"\n{ts}")
        lines.append(f"  {thought}")

    nav_row: list[dict] = []
    if page > 0:
        nav_row.append({"text": "< Prev", "callback_data": f"diary:page:{page - 1}"})
    if start + page_size < total:
        nav_row.append({"text": "Next >", "callback_data": f"diary:page:{page + 1}"})

    return _truncate_3900("\n".join(lines)), nav_row


# ---------------------------------------------------------------------------
# /links — bookmark shelf search + pagination
# ---------------------------------------------------------------------------

_LINKS_CHUNK_SIZE = 4000


def format_links(query: str | None = None) -> list[str]:
    """Return a list of text chunks for /links [query].

    No query → all recent links. Query → FTS search.
    Each chunk is at most 4000 chars, split on line boundaries.
    """
    from tools.link_shelf import db as _shelf_db

    try:
        if query:
            all_results = _shelf_db.search(query=query, limit=1000)
        else:
            all_results = _shelf_db.list_links(limit=1000)
    except Exception as exc:
        return [f"error fetching links: {exc}"]

    if not all_results:
        if query:
            return [f"no links matching {query!r}."]
        return ["no saved links."]

    header = "links — " + (f"'{query}'" if query else "recent") + f" ({len(all_results)}):"
    lines = [header]
    for link in all_results:
        lid = link.get("id") or "?"
        url = link.get("url") or "?"
        title = (link.get("title") or url)[:60]
        kind = link.get("kind") or "later"
        added = (link.get("added_at") or "")[:10]
        lines.append(f"  #{lid} [{kind}] {added}  {title}")
        lines.append(f"       {url[:80]}")

    full_text = "\n".join(lines)
    if len(full_text) <= _LINKS_CHUNK_SIZE:
        return [full_text]

    chunks: list[str] = []
    current_lines: list[str] = []
    current_len = 0
    for line in lines:
        line_len = len(line) + 1
        if current_lines and current_len + line_len > _LINKS_CHUNK_SIZE:
            chunks.append("\n".join(current_lines))
            current_lines = []
            current_len = 0
        current_lines.append(line)
        current_len += line_len
    if current_lines:
        chunks.append("\n".join(current_lines))
    return chunks


# ---------------------------------------------------------------------------
# /receipt — day/week ASCII receipt + category filter buttons
# ---------------------------------------------------------------------------

def format_receipt(view: str = "today") -> tuple[str, list[dict]]:
    """Return (text, category_keyboard_row) for /receipt [today|week|made|moved|learned|avoided].

    text: ASCII receipt from receipt_print / receipt_week.
    keyboard_row: [Today] [Week] [Made] [Moved] [Learned] [Avoided] filter buttons.
    """
    view = (view or "today").lower().strip()

    text = ""
    try:
        import asyncio as _asyncio
        from datetime import date as _date

        if view in ("today", "made", "moved", "learned", "avoided"):
            from tools.day_receipt import _db as _receipt_db
            from tools.day_receipt._render import RenderOptions, render_receipt
            r = _receipt_db.get_receipt(_date.today())
            if view in ("made", "moved", "learned", "avoided"):
                # filter to single category
                from tools.day_receipt._db import Receipt
                filtered_entries = tuple(e for e in r.entries if e.category == view)
                r = Receipt(
                    receipt_date=r.receipt_date,
                    entries=filtered_entries,
                    note=r.note,
                )
            text = render_receipt(r, RenderOptions(width=46))
            if not text.strip():
                text = f"nothing logged under '{view}' today."
        elif view == "week":
            from datetime import timedelta
            from tools.day_receipt import _db as _receipt_db
            from tools.day_receipt._render import RenderOptions, render_week
            end = _date.today()
            receipts = []
            for offset in range(6, -1, -1):
                d = end - timedelta(days=offset)
                rr = _receipt_db.get_receipt(d)
                if rr.entries or rr.note:
                    receipts.append(rr)
            text = render_week(receipts, RenderOptions(width=46))
            if not text.strip():
                text = "nothing logged this week."
        else:
            text = f"unknown view {view!r}. use: today / week / made / moved / learned / avoided"
    except Exception as exc:
        text = f"receipt error: {exc}"

    # category buttons always shown
    keyboard_row = [
        {"text": "Today", "callback_data": "receipt:today"},
        {"text": "Week", "callback_data": "receipt:week"},
        {"text": "Made", "callback_data": "receipt:made"},
        {"text": "Moved", "callback_data": "receipt:moved"},
        {"text": "Learned", "callback_data": "receipt:learned"},
        {"text": "Avoided", "callback_data": "receipt:avoided"},
    ]
    return _truncate_3900(text), keyboard_row


# ---------------------------------------------------------------------------
# /decision — list pending predictions / resolve
# ---------------------------------------------------------------------------

def format_decision(subcmd: str | None = None, args: list[str] | None = None) -> str:
    """List pending decision_log entries or resolve one.

    /decision                → list all pending (unresolved) decisions
    /decision pending        → same
    /decision resolve <id> <0|1>  → resolve a decision by id
    """
    from storage import db as _db
    args = args or []
    subcmd = (subcmd or "pending").lower()

    if subcmd == "resolve":
        if len(args) < 2:
            return "usage: /decision resolve <id> <0|1>"
        try:
            did = int(args[0])
            outcome = int(args[1])
        except ValueError:
            return f"invalid args: id={args[0]!r} outcome={args[1]!r}. use integers."
        if outcome not in (0, 1):
            return "outcome must be 0 (false) or 1 (true)."
        try:
            _db.decision_resolve(did, outcome)
        except ValueError as exc:
            return f"error resolving decision #{did}: {exc}"
        except Exception as exc:
            return f"unexpected error: {exc}"
        return f"decision #{did} resolved as {'true (1)' if outcome else 'false (0)'}."

    # pending / list
    try:
        with _db._conn() as c:
            rows = c.execute(
                "SELECT id, statement, predicted_p, resolve_by, outcome "
                "FROM decisions WHERE outcome IS NULL ORDER BY resolve_by ASC LIMIT 20"
            ).fetchall()
    except Exception as exc:
        return f"error fetching decisions: {exc}"

    if not rows:
        return "no pending (unresolved) decisions."

    lines = [f"pending decisions ({len(rows)}):"]
    for r in rows:
        did = r["id"]
        stmt = (r["statement"] or "")[:80]
        p = f"{float(r['predicted_p']):.0%}"
        resolve_by = (r["resolve_by"] or "?")[:10]
        lines.append(f"  #{did}  [{p}]  by {resolve_by}  {stmt}")
    lines.append("\nresolve with: /decision resolve <id> <0|1>")
    return _truncate_3900("\n".join(lines))


# ---------------------------------------------------------------------------
# /voice — last transcript + STT health + 3 recent voice prompts
# ---------------------------------------------------------------------------

def format_voice() -> str:
    """Show last voice-note transcript, Whisper health, and 3 recent voice prompts."""
    from storage import db as _db
    lines: list[str] = []

    # STT health
    try:
        from agents import config as _cfg
        endpoint = _cfg.get("voice.transcribe_endpoint") or ""
        model = _cfg.get("voice.transcribe_model") or ""
        key_env = _cfg.get("voice.transcribe_api_key_env") or ""
        key_set = bool(os.environ.get(str(key_env))) if key_env else False
        enabled = bool(_cfg.get("voice.enabled", True))
        lines.append("STT health:")
        lines.append(f"  enabled:  {enabled}")
        lines.append(f"  endpoint: {endpoint or '(not set)'}")
        lines.append(f"  model:    {model or '(not set)'}")
        lines.append(f"  key env:  {key_env or '(not set)'}  {'✓ set' if key_set else '✗ missing'}")
        reachable = False
        if endpoint and key_set:
            import urllib.request
            try:
                urllib.request.urlopen(endpoint.replace("/audio/transcriptions", ""), timeout=2)
                reachable = True
            except Exception:
                pass
        lines.append(f"  reachable: {'yes' if reachable else 'no (timeout or error)'}")
    except Exception as exc:
        lines.append(f"STT health: error ({exc})")

    # Last voice-note transcript — look in recent messages for [voice note] prefix
    try:
        msgs = _db.recent_messages(limit=100, exclude_ephemeral=True)
        voice_msgs = [
            m for m in reversed(msgs)
            if "[voice note" in (m.get("content") or "").lower()
        ]
        if voice_msgs:
            last = voice_msgs[-1]
            ts = (last.get("ts") or "")[:16]
            content = (last.get("content") or "")[:300]
            lines.append(f"\nlast voice note ({ts}):")
            lines.append(f"  {content}")
        else:
            lines.append("\nno voice notes in recent history.")
    except Exception as exc:
        lines.append(f"\nvoice notes: error ({exc})")

    # 3 most recent voice-style user messages (prompts that came from voice notes)
    try:
        msgs = _db.recent_messages(limit=100, exclude_ephemeral=True)
        # user messages that contain the voice note prefix
        voice_turns = [
            m for m in reversed(msgs)
            if m.get("role") == "user" and "[voice note" in (m.get("content") or "").lower()
        ][-3:]
        if voice_turns:
            lines.append(f"\nrecent voice prompts ({len(voice_turns)}):")
            for m in voice_turns:
                ts = (m.get("ts") or "")[:16]
                snippet = (m.get("content") or "")[:100]
                lines.append(f"  {ts}  {snippet}")
    except Exception as exc:
        lines.append(f"\nrecent voice prompts: error ({exc})")

    return _truncate_3900("\n".join(lines))


# ---------------------------------------------------------------------------
# /reminders pagination — 10/page + prev/next + per-item snooze/cancel
# ---------------------------------------------------------------------------

_REMINDERS_PAGE_SIZE = 10


def format_reminders_page(
    page: int = 0,
) -> tuple[str, list[dict]]:
    """Return (header_text, keyboard_rows) for /reminders page N.

    keyboard_rows: per-item [Snooze 1h] [Cancel] rows + pagination row.
    Bridge-master registers rem:snooze and rem:cancel callbacks.
    """
    from storage import db as _db
    rows = _db.reminder_list(active_only=True)
    total = len(rows)

    if total == 0:
        return "no active reminders.", []

    page_size = _REMINDERS_PAGE_SIZE
    total_pages = max(1, (total + page_size - 1) // page_size)
    start = page * page_size
    page_rows = rows[start : start + page_size]

    lines = [f"reminders — page {page + 1}/{total_pages} ({total} total):"]
    keyboard_rows: list[list[dict]] = []

    for r in page_rows:
        rid = r["id"]
        fire_at = (r.get("fire_at") or "")[:16]
        text_label = (r.get("text") or f"reminder {rid}")[:60]
        lines.append(f"  #{rid} {fire_at}  {text_label}")
        keyboard_rows.append([
            {"text": "Snooze 10m", "callback_data": f"rem:snooze:{rid}:10m"},
            {"text": "Snooze 1h",  "callback_data": f"rem:snooze:{rid}:1h"},
            {"text": "Cancel",     "callback_data": f"rem:cancel:{rid}"},
        ])

    # pagination nav row
    nav_row: list[dict] = []
    if page > 0:
        nav_row.append({"text": "< Prev", "callback_data": f"rem:page:{page - 1}"})
    if start + page_size < total:
        nav_row.append({"text": "Next >", "callback_data": f"rem:page:{page + 1}"})
    if nav_row:
        keyboard_rows.append(nav_row)

    return _truncate_3900("\n".join(lines)), keyboard_rows


# ---------------------------------------------------------------------------
# /checkin force helpers
# ---------------------------------------------------------------------------

async def run_checkin_force(send_fn) -> str:
    """Bypass the time-window guard and run the daily check-in immediately.

    ``send_fn`` has the same signature as daily_checkin's ``send_text``.
    Returns a one-line status string.
    """
    try:
        from agents import daily_checkin as _ci
        text = await _ci.compose_checkin_question()
        if not text:
            return "checkin: composer returned no question."
        from agents.proactive_gate import reserve_and_send
        today = datetime.now(UTC).date()
        result = await reserve_and_send(
            send_text_fn=send_fn,
            producer_id="daily_checkin",
            pattern="ceremony",
            text=text,
            payload_json="{}",
            candidate={
                "anchor": today.isoformat(),
                "why_now": "manual checkin from cockpit",
                "suggested_action": "yes/no/skip",
                "confidence": 0.95,
                "controls": {},
                "data_checked": ["sessions"],
            },
        )
        if result.status == "sent":
            from agents import cadence
            cadence.record_ceremony_sent("daily_checkin")
            return "checkin sent (forced)."
        return f"checkin: proactive gate blocked ({result.reason})."
    except Exception as exc:
        return f"checkin force error: {exc}"
