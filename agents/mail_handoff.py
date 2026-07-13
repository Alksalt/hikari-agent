"""Job-search mail action adapter with a Markdown compatibility fallback.

The job-search repository owns the action schema and all state transitions.
Hikari talks to that owner exclusively through ``mail_actions_cli.py``; it
never opens or writes ``job_search.db`` directly.

If the owner CLI is unavailable, the legacy Markdown handoff remains usable.
Legacy entries never expire silently.  A successful Telegram delivery changes
their status to ``surfaced`` (not ``processed``): urgent/important entries then
repeat, while normal/low entries surface once.  The old ``mark_processed``
function name is retained as a compatibility shim for existing callers, but
its semantics are deliberately mark-surfaced only.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from agents import config as cfg

logger = logging.getLogger(__name__)

_ENTRY = re.compile(
    r"^- \[(?P<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2})\] "
    r"(?P<summary>.+?) — status: (?P<status>unprocessed|surfaced(?: \S+)?)\s*$"
)
_DETAIL_PREFIX = "    - "
_PRIORITY_URGENT = 0
_PRIORITY_IMPORTANT = 1
_ATTENTION_CLASSES = {"push_now", "silent_hold", "silent_file"}


def _path() -> Path | None:
    if not cfg.get("mail_handoff.enabled", True):
        return None
    raw = str(cfg.get("mail_handoff.path", "") or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_file() else None


def _cli_path() -> Path | None:
    if not cfg.get("mail_actions.enabled", True):
        return None
    configured = str(cfg.get("mail_actions.cli_path", "") or "").strip()
    if configured:
        path = Path(configured).expanduser()
    else:
        root = str(cfg.get("jobhunt.roots.job_search", "") or "").strip()
        path = Path(root).expanduser() / "mail_actions_cli.py" if root else Path()
    return path if path.is_file() else None


def _invoke_cli(*args: str):
    """Run the narrow owner CLI without a shell; return the raw
    ``CompletedProcess``, or ``None`` if the process boundary itself was
    unavailable (missing CLI file, spawn/timeout failure).

    Unlike ``_run_cli`` below, a non-zero exit code is NOT collapsed to
    ``None`` here — callers that must distinguish "unavailable" from "the
    owner explicitly rejected this transition" (``decide``) inspect
    ``result.returncode``/``result.stderr`` themselves.
    """
    cli = _cli_path()
    if cli is None:
        return None
    timeout = max(1.0, float(cfg.get("mail_actions.timeout_seconds", 10)))
    try:
        return subprocess.run(
            [sys.executable, str(cli), *args],
            cwd=str(cli.parent),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        logger.exception("mail_actions: owner CLI failed to start/finish")
        return None


def _run_cli(*args: str, expect_json: bool = False):
    """Run the narrow owner CLI without a shell.

    ``None`` means the process boundary was unavailable or failed.  Callers
    preserve pending state on every failure so a later brief can retry.
    """
    result = _invoke_cli(*args)
    if result is None:
        return None
    if result.returncode != 0:
        logger.error(
            "mail_actions: owner CLI exited %s: %s",
            result.returncode,
            (result.stderr or "").strip()[:500],
        )
        return None
    if not expect_json:
        return True
    try:
        return json.loads(result.stdout)
    except (TypeError, ValueError):
        logger.error("mail_actions: owner CLI returned invalid JSON")
        return None


def _structured_actions() -> list[dict] | None:
    """Return normalized owner actions; ``None`` activates legacy fallback."""
    low_cap = max(0, int(cfg.get("mail_actions.low_priority_cap", 5)))
    payload = _run_cli("list", "--low-priority-cap", str(low_cap), expect_json=True)
    if payload is None:
        return None
    if not isinstance(payload, list):
        logger.error("mail_actions: owner CLI JSON was not a list")
        return None
    out: list[dict] = []
    for row in payload:
        if not isinstance(row, dict):
            logger.warning("mail_actions: ignored malformed action row")
            continue
        try:
            action_id = int(row["id"])
            priority = int(row.get("priority", 2))
        except (KeyError, TypeError, ValueError):
            logger.warning("mail_actions: ignored action without valid id/priority")
            continue
        details = row.get("details")
        if not isinstance(details, list):
            details = []
        # Task 6 (ask-user question loop): options passed through so
        # daily_brief's composer can render kind='ask-user' handoff entries
        # as numbered questions instead of the generic summary line.
        options = row.get("options")
        if not isinstance(options, list):
            options = []
        headline = str(row.get("headline") or row.get("kind") or "mail action")
        raw_attention = row.get("attention_class")
        if raw_attention in _ATTENTION_CLASSES:
            attention_class = str(raw_attention)
        elif raw_attention in (None, ""):
            # Compatibility only for rows created before attention_class.
            attention_class = (
                "push_now" if priority == _PRIORITY_URGENT
                else "silent_hold" if priority == _PRIORITY_IMPORTANT
                else "silent_file"
            )
        else:
            # Unknown explicit values must never become user-visible by
            # accidentally falling back to priority.
            attention_class = "silent_hold"
        out.append({
            "action_id": action_id,
            "source": "structured",
            "stamp": str(row.get("created_at") or ""),
            "summary": headline,
            "details": [str(item) for item in details[:8]],
            "kind": str(row.get("kind") or ""),
            "priority": priority,
            "attention_class": attention_class,
            "surface_count": int(row.get("surface_count") or 0),
            "options": [
                {"id": str(opt.get("id") or ""), "label": str(opt.get("label") or "")}
                for opt in options[:16] if isinstance(opt, dict)
            ],
        })
    return out


def _legacy_priority(summary: str) -> int:
    """Mirror the owner's coarse priority mapping without importing its DB code."""
    kind = summary.split(":", 1)[0].strip().lower()
    if any(token in kind for token in (
        "intervju", "interview", "offer", "tilbud", "assessment", "test",
        "dokument", "reference", "referanse", "pipeline-krasj",
    )):
        return _PRIORITY_URGENT
    if kind in {"svar", "bounce", "avslag-offentlig", "frist-varsel", "varsling-feil"}:
        return _PRIORITY_IMPORTANT
    if kind in {"status", "avslag", "deadline", "frist"}:
        return 2
    return 3


def _pull_legacy() -> list[dict]:
    path = _path()
    if path is None:
        return []
    low_cap = max(0, int(cfg.get("mail_actions.low_priority_cap",
                                 cfg.get("mail_handoff.max_entries", 5))))
    urgent: list[dict] = []
    low: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        logger.exception("mail_handoff: read failed")
        return []
    for i, line in enumerate(lines):
        match = _ENTRY.match(line)
        if not match:
            continue
        summary = match["summary"].strip()
        priority = _legacy_priority(summary)
        # Important legacy rows repeat after surfacing.  Lower priority rows
        # surface once, matching mail_state.pending_actions().
        if match["status"].startswith("surfaced") and priority > _PRIORITY_IMPORTANT:
            continue
        details = []
        for sub in lines[i + 1:]:
            if not sub.startswith(_DETAIL_PREFIX):
                break
            details.append(sub[len(_DETAIL_PREFIX):].strip())
        entry = {
            "raw": line,
            "source": "legacy",
            "stamp": match["stamp"],
            "summary": summary,
            "details": details[:4],
            "kind": summary.split(":", 1)[0].strip().lower(),
            "priority": priority,
            "attention_class": (
                "push_now" if priority == _PRIORITY_URGENT
                else "silent_hold" if priority == _PRIORITY_IMPORTANT
                else "silent_file"
            ),
            "surface_count": 1 if match["status"].startswith("surfaced") else 0,
        }
        (urgent if priority <= _PRIORITY_IMPORTANT else low).append(entry)
    return [*urgent, *low[:low_cap]]


def pull_unprocessed() -> list[dict]:
    """Return pending actions, prioritizing the structured owner CLI.

    Urgent/important actions are uncapped and repeat until acknowledged.
    Normal/low actions are capped and surface once.  Legacy fallback entries
    have no age cutoff: old unresolved mail must not disappear silently.
    """
    structured = _structured_actions()
    if structured is not None:
        return structured
    return _pull_legacy()


def format_lines(entries: list[dict]) -> str:
    """Compact plain-text block for prompts and diagnostics."""
    parts = []
    for entry in entries:
        details = entry.get("details") or []
        tail = f" ({'; '.join(details)})" if details else ""
        parts.append(f"- {entry.get('summary', '')}{tail}")
    return "\n".join(parts)


def _mark_legacy_surfaced(entries: list[dict]) -> None:
    path = _path()
    legacy = [entry for entry in entries if entry.get("source") == "legacy" or "raw" in entry]
    if path is None or not legacy:
        return
    stamp = datetime.now().strftime("%Y-%m-%d")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        logger.exception("mail_handoff: re-read before mark-surfaced failed")
        return
    for entry in legacy:
        raw = str(entry.get("raw") or "")
        if "— status: unprocessed" not in raw:
            continue
        marked = raw.replace(
            "— status: unprocessed", f"— status: surfaced {stamp}", 1
        )
        text = text.replace(raw + "\n", marked + "\n", 1)
    try:
        path.write_text(text, encoding="utf-8")
    except OSError:
        logger.exception("mail_handoff: mark-surfaced write failed")


def mark_surfaced(entries: list[dict]) -> bool:
    """Record confirmed delivery without acknowledging or resolving anything."""
    action_ids = [str(entry["action_id"]) for entry in entries if entry.get("action_id") is not None]
    ok = True
    if action_ids and _run_cli("mark-surfaced", *action_ids) is None:
        ok = False
    _mark_legacy_surfaced(entries)
    return ok


def mark_processed(entries: list[dict]) -> bool:
    """Compatibility name: this intentionally performs *only* mark-surfaced."""
    return mark_surfaced(entries)


def acknowledge(action_id: int) -> bool:
    """Acknowledge one structured action through the owner CLI."""
    return _run_cli("acknowledge", str(int(action_id))) is not None


def resolve(action_id: int, note: str = "") -> bool:
    """Resolve one structured action through the owner CLI."""
    return _run_cli("resolve", str(int(action_id)), "--note", str(note)) is not None


def snooze(action_id: int, until_iso: str) -> bool:
    """Snooze one structured action through the owner CLI."""
    return _run_cli("snooze", str(int(action_id)), str(until_iso)) is not None


def decide(action_id: int, option_id: str, note: str = "") -> tuple[bool, dict | str]:
    """Record which option a human chose for an ask-user action (Task 6).

    Shells the owner CLI's ``decide <id> --option <option_id> [--note ...]``.
    Returns ``(True, row)`` on success — ``row`` is the CLI's raw
    ``record_decision`` JSON (RAW ``options_json``/``details_json`` strings,
    NOT the normalized ``_structured_actions`` shape used elsewhere in this
    module). Returns ``(False, message)`` when the CLI process boundary was
    unavailable, or when the owner CLI rejected the transition (unknown
    action, wrong kind, unknown option id, already decided/resolved — these
    surface as a non-zero exit + bokmål stderr message, per
    ``mail_actions_cli.cmd_decide``'s ``SystemExit``). Callers must surface
    *message* to the user and must NOT retry — a rejected transition is not
    transient.
    """
    args = ["decide", str(int(action_id)), "--option", str(option_id)]
    if note:
        args += ["--note", str(note)]
    result = _invoke_cli(*args)
    if result is None:
        return False, "mail_actions: CLI-en er utilgjengelig akkurat nå"
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip() or "beslutning avvist"
        logger.info(
            "mail_actions: decide rejected action_id=%s: %s", action_id, message[:500]
        )
        return False, message
    try:
        return True, json.loads(result.stdout)
    except (TypeError, ValueError):
        logger.error("mail_actions: decide CLI returned invalid JSON")
        return False, "mail_actions: ugyldig svar fra CLI"
