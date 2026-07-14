"""``reminder_create`` — schedule a future poke.

Optionally syncs to Google Calendar / Apple Reminders. The sync work
is queued in the DB (``gcal_sync_pending`` / ``apple_sync_pending``)
and drained asynchronously by the background scheduler — this tool
returns immediately.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from claude_agent_sdk import tool

from agents import config as _cfg
from storage import db
from tools._annotations import annotations_for
from tools._response import ok as _ok
from tools.reminders._shared import _VALID_REPEAT, _parse_when
from tools.reminders.recurrence import validate_rule as _validate_recurrence_rule

logger = logging.getLogger(__name__)

# Default budget cap when the user doesn't specify one per fire. Aligned
# with config/engagement.yaml runtime.scheduled_action_max_budget_usd.
_DEFAULT_ACTION_BUDGET_USD = 0.40
# Hard total-cost ceiling at create time: max_fires × budget_usd_per_fire
# must not exceed this. Prevents a malformed schedule from silently
# burning through the subscription budget.
_DEFAULT_TOTAL_BUDGET_CAP_USD = 5.0

# Autonomous action reminders are deliberately narrower than interactive
# Hikari turns.  The owner approves an exact seed plus an exact set of Notion
# tools and object IDs; the sealed envelope below carries that consent from
# creation time to every later fire.
ACTION_ALLOWED_TOOLS = frozenset({
    "mcp__notion__API-post-page",
    "mcp__notion__API-patch-page",
    "mcp__notion__API-create-a-data-source",
    "mcp__notion__API-update-a-data-source",
    "mcp__notion__API-patch-block-children",
    "mcp__notion__API-update-a-block",
    "mcp__notion__API-create-a-comment",
})
_ACTION_ENVELOPE_PREFIX = "HIKARI_ACTION_V1:"


def _normalized_string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return sorted({str(item).strip() for item in value if str(item).strip()})


def action_approval_payload(args: dict[str, Any]) -> dict[str, Any]:
    """Return the security-sensitive bytes covered by owner approval."""
    return {
        "when_iso": str(args.get("when_iso") or "").strip(),
        "text": str(args.get("text") or "").strip(),
        "kind": str(args.get("kind") or "text").strip().lower(),
        "recurrence": str(args.get("recurrence") or "").strip(),
        "max_fires": int(args.get("max_fires") or 0),
        "seed_prompt": str(args.get("seed_prompt") or "").strip(),
        "summary_prompt": str(args.get("summary_prompt") or "").strip(),
        "budget_usd_per_fire": float(args.get("budget_usd_per_fire") or 0),
        "timeout_s": int(args.get("timeout_s") or 0),
        "allowed_tools": _normalized_string_list(args.get("allowed_tools")),
        "allowed_targets": _normalized_string_list(args.get("allowed_targets")),
    }


def action_approval_sha256(args: dict[str, Any]) -> str:
    payload = action_approval_payload(args)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def encode_action_seed(
    args: dict[str, Any], *, role: str, approval_sha256: str
) -> str:
    """Seal one approved action prompt and its execution scope for storage."""
    payload = action_approval_payload(args)
    envelope = {
        "version": 1,
        "role": role,
        "approval_payload": payload,
        "approval_sha256": approval_sha256,
    }
    return _ACTION_ENVELOPE_PREFIX + json.dumps(
        envelope, sort_keys=True, separators=(",", ":")
    )


def decode_action_seed(stored: str) -> tuple[str, dict[str, Any]]:
    """Verify a stored action envelope and return (prompt, binding).

    The scheduler may append a human-readable fire counter after the first
    line.  That context is preserved, while the approved seed itself remains
    hash-bound and cannot be silently replaced between approval and firing.
    """
    first_line, separator, suffix = str(stored or "").partition("\n")
    if not first_line.startswith(_ACTION_ENVELOPE_PREFIX):
        raise ValueError("scheduled action is missing an approved seed envelope")
    try:
        envelope = json.loads(first_line[len(_ACTION_ENVELOPE_PREFIX):])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("scheduled action seed envelope is malformed") from exc
    if envelope.get("version") != 1 or envelope.get("role") not in {"seed", "summary"}:
        raise ValueError("scheduled action seed envelope has an unsupported format")
    payload = envelope.get("approval_payload")
    if not isinstance(payload, dict):
        raise ValueError("scheduled action seed envelope has no approval payload")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    actual_sha = hashlib.sha256(canonical.encode()).hexdigest()
    approved_sha = str(envelope.get("approval_sha256") or "")
    if not approved_sha or actual_sha != approved_sha:
        raise ValueError("scheduled action seed approval hash mismatch")
    role = envelope["role"]
    prompt_key = "seed_prompt" if role == "seed" else "summary_prompt"
    prompt = str(payload.get(prompt_key) or "").strip()
    if not prompt:
        raise ValueError(f"scheduled action {role} prompt is empty")
    if separator and suffix:
        prompt = f"{prompt}\n{suffix}"
    binding = {
        "approval_sha256": approved_sha,
        "allowed_tools": frozenset(_normalized_string_list(payload.get("allowed_tools"))),
        "allowed_targets": frozenset(_normalized_string_list(payload.get("allowed_targets"))),
    }
    return prompt, binding


@tool(
    "reminder_create",
    (
        "Schedule a reminder that fires as a real Telegram push at when_iso. "
        "when_iso MUST be a fully-resolved ISO-8601 timestamp (UTC or with tz "
        "offset) — the parser refuses anything else. If the user gives a "
        "relative time, YOU compute the ISO from the `# now` block injected "
        "at the top of your context. Do not call this tool with natural-"
        "language time strings like 'in 1h' or 'tomorrow'. (If you truly "
        "cannot resolve a precise ISO, the user's phrase verbatim is "
        "attempted as a last-resort fallback — but always prefer ISO.) "
        "Examples: "
        "(EN) user 'remind me in 5 min to stretch', `# now` utc 2026-05-20T18:00:00+00:00 "
        "→ when_iso='2026-05-20T18:05:00+00:00', text='stretch'. "
        "(UK) user 'нагадай через годину написати маріку', `# now` utc 2026-05-20T18:00:00+00:00 "
        "→ when_iso='2026-05-20T19:00:00+00:00', text='написати маріку'. "
        "(RU) user 'напомни мне завтра в 9 позвонить маме', "
        "`# now` local 2026-05-20 18:00 Europe/Kyiv → "
        "when_iso='2026-05-21T09:00:00+03:00', text='позвонить маме'. "
        "text is what Hikari will say when the reminder fires. "
        "lead_minutes (default 0) fires N minutes BEFORE when_iso — useful for "
        "events ('remind me 1h before my 14:00 meeting' → when_iso=14:00, "
        "lead_minutes=60, fires at 13:00). "
        "repeat one of {daily, weekly, monthly, yearly} for simple repeats, or "
        "an RRULE string for advanced. "
        "recurrence: structured recurrence rule for smart rescheduling. "
        "Grammar: 'daily' | 'weekly:MON,WED,FRI' | 'monthly:1' | 'monthly:last' "
        "| 'yearly:MM-DD' | 'every_n_days:N'. "
        "When set the reminder auto-reschedules after each fire — it keeps "
        "looping until the user explicitly cancels it. "
        "sync_to_gcal=True queues a Google Calendar mirror (non-blocking — the "
        "GCal sync job drains the queue separately). "
        "sync_to_apple=True queues an Apple Reminders mirror (macOS only, "
        "non-blocking). "
        "kind='action' creates an autonomous-action reminder: when fire_at hits, "
        "Hikari wakes (via run_scheduled_action) and executes the work in "
        "seed_prompt. Requires recurrence + max_fires + seed_prompt. Useful for "
        "time-spanning tasks like 'write a Notion row every 20 min for 2 hours'. "
        "max_fires caps the number of fires; after the last fire, summary_prompt "
        "(if set) runs as a final wrap-up turn whose text IS pushed to Telegram. "
        "budget_usd_per_fire and timeout_s override the defaults (0.40 USD / 180 s) "
        "per fire. The total cost (max_fires × budget) is capped at create time. "
        "Action reminders also require allowed_tools (exact Notion tool IDs) and "
        "allowed_targets (exact Notion page/block/data-source IDs); every later "
        "autonomous write is constrained to that approved scope. "
        "kind='text' (default) is the existing static-text reminder."
    ),
    {"when_iso": str, "text": str, "lead_minutes": int, "repeat": str,
     "recurrence": str, "sync_to_gcal": bool, "sync_to_apple": bool,
     "kind": str, "seed_prompt": str, "max_fires": int,
     "summary_prompt": str, "budget_usd_per_fire": float, "timeout_s": int,
     "allowed_tools": list, "allowed_targets": list},
    annotations=annotations_for("reminder_create"),
)
async def reminder_create(args: dict[str, Any]) -> dict[str, Any]:
    when_iso = (args.get("when_iso") or "").strip()
    text = (args.get("text") or "").strip()
    lead_minutes = int(args.get("lead_minutes") or 0)
    repeat = (args.get("repeat") or "").strip() or None
    recurrence = (args.get("recurrence") or "").strip() or None
    sync_to_gcal = bool(args.get("sync_to_gcal", True))
    # Default True on macOS; False elsewhere (EventKit is Apple-only).
    sync_to_apple = bool(args.get("sync_to_apple", sys.platform == "darwin"))

    # Action-mode arguments.
    kind = (args.get("kind") or "text").strip().lower()
    seed_prompt = (args.get("seed_prompt") or "").strip() or None
    summary_prompt = (args.get("summary_prompt") or "").strip() or None
    max_fires_raw = args.get("max_fires")
    max_fires = int(max_fires_raw) if max_fires_raw not in (None, "", 0) else None
    budget_raw = args.get("budget_usd_per_fire")
    budget_usd_per_fire = float(budget_raw) if budget_raw not in (None, "", 0) else None
    timeout_raw = args.get("timeout_s")
    timeout_s = int(timeout_raw) if timeout_raw not in (None, "", 0) else None
    allowed_tools = _normalized_string_list(args.get("allowed_tools"))
    allowed_targets = _normalized_string_list(args.get("allowed_targets"))

    if kind not in {"text", "action"}:
        return _ok(f"refused: kind must be 'text' or 'action', got {kind!r}")

    if not text:
        return _ok("refused: empty text")
    when = _parse_when(when_iso)
    if when is None:
        return _ok(f"refused: cannot parse when_iso={when_iso!r}")
    if when - timedelta(minutes=lead_minutes) <= datetime.now(UTC):
        return _ok("refused: fire time is in the past")
    if repeat not in _VALID_REPEAT and not repeat.upper().startswith("RRULE:"):
        return _ok(
            f"refused: repeat={repeat!r} must be one of {{daily,weekly,monthly,yearly}} "
            f"or an RRULE string"
        )
    if recurrence is not None:
        try:
            _validate_recurrence_rule(recurrence)
        except ValueError as exc:
            return _ok(f"refused: {exc}")

    # Action-mode validation.
    if kind == "action":
        if not seed_prompt:
            return _ok("refused: kind='action' requires seed_prompt")
        if not recurrence:
            return _ok("refused: kind='action' requires recurrence")
        if not max_fires or max_fires < 1:
            return _ok("refused: kind='action' requires max_fires >= 1")
        # Cost cap: max_fires × per-fire budget must not exceed the
        # configured total ceiling. Refuse loudly — no silent truncation.
        per_fire = budget_usd_per_fire or _DEFAULT_ACTION_BUDGET_USD
        cap = float(_cfg.get("reminders.action_max_total_usd",
                             _DEFAULT_TOTAL_BUDGET_CAP_USD))
        total = max_fires * per_fire
        if total > cap:
            return _ok(
                f"refused: total budget ${total:.2f} ({max_fires} × ${per_fire:.2f}) "
                f"exceeds cap ${cap:.2f}. lower max_fires or budget_usd_per_fire."
            )
        if not allowed_tools:
            return _ok("refused: kind='action' requires allowed_tools")
        unsupported_tools = sorted(set(allowed_tools) - ACTION_ALLOWED_TOOLS)
        if unsupported_tools:
            return _ok(
                "refused: action allowed_tools contains unsupported tool(s): "
                + ", ".join(unsupported_tools)
            )
        if not allowed_targets:
            return _ok("refused: kind='action' requires allowed_targets")
        expected_sha = action_approval_sha256(args)
        approved_sha = str(args.get("_approved_action_sha256") or "")
        if not approved_sha or approved_sha != expected_sha:
            return _ok("refused: action reminder bytes were not explicitly approved")
        # Action reminders are background work — they must not also mirror
        # to user-visible calendars/apple-reminders (those represent the
        # *schedule* of fires, not the work). Force both off.
        sync_to_gcal = False
        sync_to_apple = False
        seed_prompt = encode_action_seed(
            args, role="seed", approval_sha256=approved_sha
        )
        if summary_prompt:
            summary_prompt = encode_action_seed(
                args, role="summary", approval_sha256=approved_sha
            )

    rid = db.reminder_insert(
        fire_at=when.isoformat(),
        text=text,
        lead_minutes=lead_minutes,
        repeat=repeat,
        recurrence_rule=recurrence,
        gcal_sync_pending=sync_to_gcal,
        apple_sync_pending=sync_to_apple,
        kind=kind,
        seed_prompt=seed_prompt,
        max_fires=max_fires,
        summary_prompt=summary_prompt,
        budget_usd_per_fire=budget_usd_per_fire,
        timeout_s=timeout_s,
    )
    if kind == "action":
        return _ok(
            f"action reminder #{rid} set for {when.isoformat()} "
            f"(recurrence {recurrence}, max_fires {max_fires}, "
            f"per-fire ${budget_usd_per_fire or _DEFAULT_ACTION_BUDGET_USD:.2f}, "
            f"summary {'yes' if summary_prompt else 'no'})",
            data={"id": rid, "kind": "action"},
        )
    return _ok(
        f"reminder #{rid} set for {when.isoformat()} "
        f"(lead {lead_minutes}m, repeat {repeat or 'none'}, "
        f"recurrence {recurrence or 'none'}, "
        f"gcal_sync {'queued' if sync_to_gcal else 'skipped'}, "
        f"apple_sync {'queued' if sync_to_apple else 'skipped'})",
        data={"id": rid},
    )
