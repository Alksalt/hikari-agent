"""Telegram cockpit — pure formatting helpers for operator commands.

All public functions return plain strings ≤3900 chars (enforced via
_truncate_3900).  No telegram objects; no DB writes except settings.set.
This separation makes every formatter unit-testable without telegram mocks.

Phase 6A — text MVP.
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

_COMMANDS: dict[str, str] = {
    "start":        "wake up / confirm i'm here",
    "silence":      "mute proactives for N minutes (default 120)",
    "unsilence":    "cancel active silence window",
    "tasks":        "list open background tasks",
    "cancel":       "cancel a running background task by id",
    "memory":       "query / edit the fact + message memory",
    "memory_diff":  "diff two memory snapshots",
    "approvals":    "list / cancel pending gatekeeper approvals",
    "proactive":    "manage proactive sources, see recent sends, snooze a source",
    "grab_stickers": "import sticker pack urls you pasted",
    "help":         "show this command list",
    "status":       "system status: uptime, scheduler, MCP, DB, cost, OAuth",
    "tools":        "list tool registry (policy) / recent tool_calls (recent) / audit_log (audit)",
    "audit":        "paginate audit_log (recent / tools / approvals / id <id>)",
    "settings":     "get or set allowlisted runtime settings",
    "reminders":    "list active reminders with snooze/dismiss buttons",
    "checkin":      "morning checkin: run now / skip tomorrow",
    "capabilities": "tool families, skill count, and MCP server health",
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
    with open(path, "w") as _f:
        yaml.dump(data, _f, default_flow_style=False, allow_unicode=True)
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
    if v not in ("openrouter", "haiku_subscription"):
        raise ValueError(f"aux_model.provider must be openrouter|haiku_subscription, got {v!r}")
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
        "doc": "hour (0-23, UTC) when quiet hours begin — no proactive messages",
    },
    "quiet_end_hour": {
        "type": "int[0-23]",
        "validate": lambda v: v.strip().isdigit() and 0 <= int(v.strip()) <= 23,
        "reader": _read_quiet_end_hour,
        "writer": _write_quiet_end_hour,
        "doc": "hour (0-23, UTC) when quiet hours end — proactives resume",
    },
    "aux_model.provider": {
        "type": "enum[openrouter,haiku_subscription]",
        "validate": lambda v: v.strip().lower() in ("openrouter", "haiku_subscription"),
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

    # DB row counts
    try:
        with _db._conn() as c:
            n_facts = c.execute(
                "SELECT COUNT(*) FROM facts WHERE valid_to IS NULL"
            ).fetchone()[0]
            n_msgs = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            n_tasks = c.execute(
                "SELECT COUNT(*) FROM tasks WHERE status='open'"
            ).fetchone()[0]
            n_eps = c.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
            n_pending_approvals = c.execute(
                "SELECT COUNT(*) FROM approvals WHERE status='pending'"
            ).fetchone()[0]
        lines.append(
            f"db: {n_facts} facts, {n_msgs} msgs, {n_eps} episodes, "
            f"{n_tasks} open tasks"
        )
        lines.append(f"pending approvals: {n_pending_approvals}")
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

    # sticker pool health
    try:
        from agents import config as _cfg
        pool = _cfg.get("stickers.pool") or []
        if not pool:
            lines.append("stickers: degraded (pool empty — /status shows no file_ids)")
        else:
            lines.append(f"stickers: {len(pool)} in pool")
    except Exception as exc:
        lines.append(f"stickers: error ({exc})")

    return _truncate_3900("\n".join(lines))


def format_tools(subcmd: str, args: list[str]) -> str:
    subcmd = (subcmd or "policy").lower()

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

    # default: policy — group by access_mode
    try:
        from tools._tools_yaml import load_registry
        reg = load_registry()
        tools_list = reg.tools()
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
        # Count skills separately
        skills_root = tools_root.parent / ".agents" / "skills"
        n_skills = len(list(skills_root.iterdir())) if skills_root.exists() else 0
        lines.append(f"\ntool families ({len(families)}):")
        for fam, count in sorted(families.items()):
            lines.append(f"  {fam}: {count} tool(s)")
        lines.append(f"  skills: {n_skills} skill(s) in .agents/skills/")
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
    from storage import db as _db
    row = _db.proactive_event_by_id(event_id)
    if row is None:
        return f"proactive event #{event_id} not found."
    lines = [
        f"proactive event #{row['id']}",
        f"  source:   {row.get('source') or '?'}",
        f"  sent_at:  {row.get('sent_at') or '?'}",
        f"  status:   {row.get('status') or '?'}",
    ]
    aborted = row.get("aborted_reason")
    if aborted:
        lines.append(f"  aborted:  {aborted}")
    preview = _payload_preview(row.get("payload_json"), max_len=200)
    if preview:
        lines.append(f"  preview:  {preview}")
    up = row.get("thumbs_up") or 0
    dn = row.get("thumbs_down") or 0
    lines.append(f"  feedback: 👍{up} 👎{dn}")
    tg_id = row.get("telegram_message_id")
    if tg_id:
        lines.append(f"  tg_msg_id: {tg_id}")
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
