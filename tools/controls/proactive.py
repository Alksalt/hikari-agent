"""``set_proactive_source`` — enable, disable, or snooze a proactive source.

Mirrors /proactive on|off|snooze logic from the Telegram bridge, writing
the same ``proactive_enabled_sources_override`` and
``proactive_snooze_until`` runtime_state keys.

Also accepts action='status' to return the current enabled/disabled/
snoozed state per source (reuses ``cockpit.format_proactive_status``).

Args:
  source: str — must be in ALL_PRODUCER_IDS. For action='status', omit
          or pass 'all' to get a full status report.
  action: 'on' | 'off' | 'snooze' | 'status'
  snooze_hours: float — required for action='snooze'. Can also be
                expressed as a string like '2h', '30m', '1d' via the
                cockpit helper.
"""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

from claude_agent_sdk import tool

from storage import db
from tools._annotations import annotations_for
from tools._response import ok as _ok

_VALID_ACTIONS = frozenset({"on", "off", "snooze", "status"})


def _load_enabled() -> set[str]:
    """Resolve current enabled set — override → yaml → code defaults."""
    from agents.engagement.producers import DEFAULT_ENABLED_SOURCES
    raw = db.runtime_get("proactive_enabled_sources_override")
    if raw:
        try:
            return set(json.loads(raw))
        except (ValueError, TypeError):
            pass
    try:
        from agents import config as _cfg
        yaml_sources = _cfg.get("proactive.default_enabled_sources")
        if yaml_sources:
            return set(yaml_sources)
    except Exception:
        pass
    return set(DEFAULT_ENABLED_SOURCES)


def _load_snooze_map() -> dict[str, str]:
    raw = db.runtime_get("proactive_snooze_until")
    try:
        m = json.loads(raw) if raw else {}
        return m if isinstance(m, dict) else {}
    except (ValueError, TypeError):
        return {}


@tool(
    "set_proactive_source",
    "Enable, disable, snooze, or check status of a proactive message source. "
    "source: id from ALL_PRODUCER_IDS (e.g. 'weather_mood_shift'). "
    "action: 'on' | 'off' | 'snooze' | 'status'. "
    "snooze_hours: hours to snooze (required for action='snooze'; e.g. 2.0). "
    "For action='status', source is optional — omit or pass any value to get "
    "the full status report.",
    {"source": str, "action": str, "snooze_hours": float},
    annotations=annotations_for("set_proactive_source"),
)
async def set_proactive_source(args: dict[str, Any]) -> dict[str, Any]:
    from agents.engagement.producers import ALL_PRODUCER_IDS

    source = (args.get("source") or "").strip()
    action = (args.get("action") or "status").strip().lower()
    snooze_hours_raw = args.get("snooze_hours")

    if action not in _VALID_ACTIONS:
        return _ok(
            f"refused: action must be one of {sorted(_VALID_ACTIONS)}, got {action!r}"
        )

    if action == "status":
        try:
            from agents.cockpit import format_proactive_status
            return _ok(format_proactive_status())
        except Exception as exc:
            return _ok(f"status error: {exc}")

    # on / off / snooze all need a valid source
    if not source:
        return _ok(
            f"refused: source is required for action={action!r}. "
            f"valid sources: {sorted(ALL_PRODUCER_IDS)}"
        )
    if source not in ALL_PRODUCER_IDS:
        return _ok(
            f"refused: unknown source {source!r}. "
            f"valid sources: {sorted(ALL_PRODUCER_IDS)}"
        )

    if action == "on":
        enabled = _load_enabled()
        enabled.add(source)
        db.runtime_set(
            "proactive_enabled_sources_override",
            json.dumps(sorted(enabled)),
        )
        return _ok(
            f"on: {source}. enabled sources: {sorted(enabled)}",
            data={"source": source, "action": "on", "enabled": sorted(enabled)},
        )

    if action == "off":
        enabled = _load_enabled()
        enabled.discard(source)
        db.runtime_set(
            "proactive_enabled_sources_override",
            json.dumps(sorted(enabled)),
        )
        msg = f"off: {source}. enabled sources: {sorted(enabled)}"
        if not enabled:
            msg += (
                " — that was the last enabled source — "
                "ALL proactive messages are now off (use action='on' to re-enable)."
            )
        return _ok(
            msg,
            data={"source": source, "action": "off", "enabled": sorted(enabled)},
        )

    # action == 'snooze'
    try:
        hours = float(snooze_hours_raw) if snooze_hours_raw is not None else 0.0
    except (ValueError, TypeError):
        hours = 0.0
    if hours <= 0:
        return _ok("refused: snooze_hours must be > 0 for action='snooze'")

    snooze_map = _load_snooze_map()
    until_epoch = time.time() + hours * 3600
    until_iso = datetime.fromtimestamp(until_epoch, tz=UTC).isoformat()
    snooze_map[source] = until_iso
    db.runtime_set("proactive_snooze_until", json.dumps(snooze_map))

    h = int(hours)
    m = int((hours - h) * 60)
    dur_str = f"{h}h" if m == 0 else f"{h}h {m}m"
    return _ok(
        f"snoozed {source} for {dur_str} (until {until_iso[:16]} UTC).",
        data={"source": source, "action": "snooze", "until": until_iso},
    )
