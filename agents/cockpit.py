"""Telegram cockpit — pure formatting helpers for inline-keyboard flows.

Phase 5b (useful-agent pivot): the slash-command surface is gone. What
remains here are the formatters still consumed by surviving callers:

- ``format_proactive_status`` — reused by the ``set_proactive_source``
  conversational tool (action='status').
- ``format_proactive_why`` / ``format_proactive_snooze`` — pro:/why/snooze
  callback handlers.
- ``_parse_duration`` — shared duration parsing for reminder snooze.

All public functions return plain strings ≤3900 chars (enforced via
_truncate_3900).  No telegram objects; no DB writes except snooze state.
"""
from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _truncate_3900(text: str) -> str:
    """Single chokepoint — all public formatters pipe through here."""
    limit = 3900
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…[truncated]"


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
        from agents.engagement.producers import DEFAULT_ENABLED_SOURCES
    except Exception:
        DEFAULT_ENABLED_SOURCES = []

    # enabled sources — mirror the scheduler's resolution: override → yaml → code.
    raw_override = _db.runtime_get("proactive_enabled_sources_override")
    if raw_override:
        try:
            enabled: set[str] = set(json.loads(raw_override))
        except (ValueError, TypeError):
            enabled = set(DEFAULT_ENABLED_SOURCES)
    else:
        from agents import config as _cfg_src
        _yaml_sources = _cfg_src.get("proactive.default_enabled_sources")
        enabled = set(_yaml_sources) if _yaml_sources else set(DEFAULT_ENABLED_SOURCES)

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

    # disabled sources — so the owner can see what's available to turn on.
    try:
        from agents.engagement.producers import ALL_PRODUCER_IDS
        disabled = sorted(s for s in ALL_PRODUCER_IDS if s not in enabled)
    except Exception:
        disabled = []
    if disabled:
        lines.append(
            f"\ndisabled ({len(disabled)}) — enable via set_proactive_source:"
        )
        lines.append("  " + ", ".join(disabled))

    return _truncate_3900("\n".join(lines))


def format_proactive_snooze(source: str, duration_str: str) -> str:
    from storage import db as _db
    secs = _parse_duration(duration_str)
    if secs is None:
        return f"can't parse duration {duration_str!r}. use e.g. 30m / 2h / 1d"

    # Validate source against known producer ids (skip for the global "all" sentinel)
    if source != "all":
        try:
            from agents.engagement.producers import ALL_PRODUCER_IDS
        except Exception:
            ALL_PRODUCER_IDS = frozenset()
        if source not in ALL_PRODUCER_IDS:
            return (
                f"unknown source: {source} — see set_proactive_source "
                f"status for valid ids"
            )

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

