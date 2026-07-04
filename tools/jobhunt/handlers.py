"""Job-hunt tool handlers — invoked lazily on first call.

Three read-only views over ``tools/jobhunt/readers.py`` (the typed
SQLite/markdown adapters from Task 1) — ``radar``/``org``/``prep``. No
handler here ever writes to outreach.db / job_search.db / Notion — same
hard read-only contract as the readers module. A fourth handler,
``draft_touch`` (Task 4), is the one exception: it creates a Gmail
**draft** (never sends) via ``tools/jobhunt/drafter.py`` — outreach.db /
job_search.db / Notion themselves are still never written.

Wrapping policy (mirrors ``tools/link_shelf/handlers.py``'s selective
field-level wrapping, NOT the whole-payload PostToolUse hook pattern
``query_inbox``/``weather_fetch`` rely on): each renderer wraps only the
free-text fields that plausibly originated outside the owner's own
typing — organisation names, contact names, "warm hook" notes, note
tails, job titles, and prep-doc bodies. Dates, counts, touch labels,
slugs, and status/stage strings are short structured values the owner
picked from a closed vocabulary (or wrote about themselves) and are left
unwrapped so the model can reason about scheduling without fighting
through delimiter noise. ``config/tools.yaml`` ALSO sets
``wrap_patterns`` on all four ids (mirroring ``query_inbox``'s exact
syntax, per the task brief) — that's a second, coarser defense-in-depth
layer applied by the PostToolUse hook at the real MCP boundary; it has
no effect on the direct handler-level unit tests in this repo, which
call these functions without going through the SDK hook chain.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from agents.injection_guard import wrap_untrusted
from tools._response import ok as _ok
from tools.jobhunt import drafter, readers

logger = logging.getLogger(__name__)

_RADAR_TOOL = "mcp__hikari_utility__jobhunt_radar"
_ORG_TOOL = "mcp__hikari_utility__jobhunt_org"
_PREP_TOOL = "mcp__hikari_utility__jobhunt_prep"

_SECTION_CAP = 5
_PREP_EXCERPT_CHARS = 300


def _today() -> date:
    """Local (HOME_TZ) date — mirrors ``tools/controls/checkin.py``'s
    local import of ``agents.daily_checkin._resolve_local_tz`` so midnight
    boundaries match the same wall-clock the rest of the agent uses."""
    from datetime import datetime as _dt

    from agents.daily_checkin import _resolve_local_tz
    return _dt.now(_resolve_local_tz()).date()


# ---------- jobhunt_radar ----------


def _safe_due_entry(entry: dict[str, Any]) -> dict[str, Any]:
    safe = dict(entry)
    for key in ("org", "kontakt", "varm_hook", "notater_tail"):
        if safe.get(key):
            safe[key] = wrap_untrusted(_RADAR_TOOL, safe[key])
    return safe


def _safe_deadline_entry(entry: dict[str, Any]) -> dict[str, Any]:
    safe = dict(entry)
    for key in ("org", "stilling"):
        if safe.get(key):
            safe[key] = wrap_untrusted(_RADAR_TOOL, safe[key])
    return safe


def _safe_interview_entry(entry: dict[str, Any]) -> dict[str, Any]:
    safe = dict(entry)
    if safe.get("org"):
        safe["org"] = wrap_untrusted(_RADAR_TOOL, safe["org"])
    return safe


def _fmt_due(e: dict[str, Any]) -> str:
    contact = f" [{e['kontakt']}]" if e.get("kontakt") else ""
    hook = f" — hook: {e['varm_hook']}" if e.get("varm_hook") else ""
    return (
        f"{e['org']}{contact} — touch {e['touch']}, due {e['due']}"
        f" ({e['days_overdue']}d overdue){hook}"
    )


def _fmt_deadline(e: dict[str, Any]) -> str:
    role = f" ({e['stilling']})" if e.get("stilling") else ""
    return f"{e['org']}{role} — frist {e['frist']}"


def _fmt_interview(e: dict[str, Any]) -> str:
    when = e.get("date") or "date TBD"
    stage = f" [{e['prep_state']}]" if e.get("prep_state") else ""
    return f"{e['org']} ({e['slug']}) — {when} via {e['source']}{stage}"


def _render_section(title: str, items: list[dict[str, Any]], formatter) -> list[str]:
    total = len(items)
    out = [f"\n{title} ({total}):"]
    if not items:
        out.append("  none")
        return out
    for item in items[:_SECTION_CAP]:
        out.append(f"  - {formatter(item)}")
    if total > _SECTION_CAP:
        out.append(f"  ...+{total - _SECTION_CAP} more")
    return out


def _fmt_pipeline(summary: dict[str, Any]) -> str:
    out_counts = ", ".join(f"{k}={v}" for k, v in (summary.get("outreach") or {}).items())
    app_counts = ", ".join(f"{k}={v}" for k, v in (summary.get("applications") or {}).items())
    return f"\npipeline: outreach [{out_counts or 'none'}] | applications [{app_counts or 'none'}]"


async def radar(args: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG001 — no args, kept for lazy_tool signature
    today = _today()
    due = [_safe_due_entry(e) for e in readers.outreach_due(today)]
    deadlines = [_safe_deadline_entry(e) for e in readers.application_deadlines(today)]
    interviews = [_safe_interview_entry(e) for e in readers.interviews_upcoming(today)]
    summary = readers.pipeline_summary()

    lines = ["jobhunt radar:"]
    lines += _render_section("outreach due", due, _fmt_due)
    lines += _render_section("application deadlines", deadlines, _fmt_deadline)
    lines += _render_section("interviews upcoming", interviews, _fmt_interview)
    lines.append(_fmt_pipeline(summary))

    # data is capped to the same _SECTION_CAP slice the narrative shows:
    # tools._response.ok JSON-dumps `data` into the tool text, and every row
    # carries 4 wrap_untrusted banners — 71 uncapped rows once produced a
    # 174KB result that blew the SDK's 25k-token MCP output cap, leaving the
    # model with only a size-limit error. Totals keep the full counts
    # queryable; per-org detail goes through jobhunt_org.
    data = {
        "outreach_due": due[:_SECTION_CAP],
        "application_deadlines": deadlines[:_SECTION_CAP],
        "interviews_upcoming": interviews[:_SECTION_CAP],
        "totals": {
            "outreach_due": len(due),
            "application_deadlines": len(deadlines),
            "interviews_upcoming": len(interviews),
        },
        "pipeline_summary": summary,
    }
    return _ok("\n".join(lines), data=data, presentation_hint="list_of_records")


# ---------- jobhunt_org ----------

# Raw sqlite column names (org_context returns the native outreach.db row,
# per readers.py's key contract) that hold text plausibly sourced from
# outside the owner's own typing. Superset of the org/kontakt/varm_hook/
# notater_tail set jobhunt_radar wraps (notater_tail -> notater, since
# org_context returns the full field): the real DB also holds scraped
# contact blocks in ekstra_kontakter, pasted job titles in kontakt_rolle,
# and external URLs / pasted LinkedIn text in kontakt_kilde / nettside.
# kontakt_epost deliberately stays bare — same contact-emails-unwrapped
# decision as the radar renderer.
_ORG_WRAP_COLUMNS = (
    "organisasjon", "kontaktperson", "varm_hook", "notater",
    "ekstra_kontakter", "kontakt_rolle", "kontakt_kilde", "nettside",
)


def _safe_org_row(row: dict[str, Any]) -> dict[str, Any]:
    safe = dict(row)
    for key in _ORG_WRAP_COLUMNS:
        if safe.get(key):
            safe[key] = wrap_untrusted(_ORG_TOOL, safe[key])
    return safe


async def org(args: dict[str, Any]) -> dict[str, Any]:
    name = (args.get("name") or "").strip()
    if not name:
        return _ok("refused: jobhunt_org needs a name")

    ctx = readers.org_context(name)
    if ctx is None:
        return _ok(f"no outreach row matches '{name}'")

    if "ambiguous" in ctx:
        candidates = ctx["ambiguous"]
        safe_candidates = [wrap_untrusted(_ORG_TOOL, c) for c in candidates]
        lines = [f"'{name}' matches {len(candidates)} organisations — be more specific:"]
        lines += [f"  - {c}" for c in safe_candidates]
        return _ok(
            "\n".join(lines),
            data={"ambiguous": safe_candidates},
            presentation_hint="list_of_records",
        )

    safe = _safe_org_row(ctx)
    lines = [f"org context for {safe.get('organisasjon') or name}:"]
    for key, value in safe.items():
        if key == "id" or value in (None, ""):
            continue
        lines.append(f"  {key}: {value}")
    return _ok("\n".join(lines), data=safe, presentation_hint="scalar")


# ---------- jobhunt_prep ----------

_PREP_TEXT_KEYS = (
    ("company_dossier", "dossier"),
    ("positioning", "positioning"),
    ("interview_plan", "interview plan"),
)


def _excerpt(text: str, n: int = _PREP_EXCERPT_CHARS) -> str:
    text = text or ""
    return text[:n] + ("…" if len(text) > n else "")


async def prep(args: dict[str, Any]) -> dict[str, Any]:
    slug = (args.get("slug") or "").strip()
    if not slug:
        return _ok("refused: jobhunt_prep needs a slug")

    files = readers.prep_files(slug)
    if not files:
        return _ok(f"no prep folder found for '{slug}'")

    present = [key for key, _label in _PREP_TEXT_KEYS if files.get(key)]
    stories = files.get("confirmed_stories") or []
    tier = files.get("tier") or "(no tier line)"

    lines = [
        f"prep for '{slug}':",
        f"  tier: {tier}",
        f"  files present: {', '.join(present) or 'none'}",
        f"  confirmed stories: {len(stories)}",
    ]

    safe_data: dict[str, Any] = {
        "tier": tier,
        "files_present": present,
        "confirmed_story_count": len(stories),
    }

    for key, label in _PREP_TEXT_KEYS:
        raw_text = files.get(key)
        if not raw_text:
            continue
        safe_data[key] = wrap_untrusted(_PREP_TOOL, raw_text)
        lines.append(f"\n  {label} excerpt:\n    {wrap_untrusted(_PREP_TOOL, _excerpt(raw_text))}")

    if stories:
        safe_stories = [wrap_untrusted(_PREP_TOOL, s) for s in stories]
        safe_data["confirmed_stories"] = safe_stories

    return _ok("\n".join(lines), data=safe_data, presentation_hint="scalar")


# ---------- jobhunt_draft_touch ----------
#
# The one write in this file: composes a touch email, gates it through
# tools/jobhunt/lint.py's deterministic rails, and — only on a clean pass —
# creates a Gmail draft (never sends). Wrapping is done inside
# tools/jobhunt/drafter.py itself (org name / notater tail are wrapped
# before they land in either the narrative text or `data`, same
# data-must-already-be-wrapped contract as the three renderers above)
# because drafter.draft_touch() is also unit-tested directly in
# tests/test_jobhunt_drafter.py and must return already-safe text on its
# own, independent of this thin handler wrapper.


async def draft_touch(args: dict[str, Any]) -> dict[str, Any]:
    org = (args.get("org") or "").strip()
    touch = (args.get("touch") or "").strip()
    result = await drafter.draft_touch(org, touch)
    return _ok(result["text"], data=result.get("data"))
