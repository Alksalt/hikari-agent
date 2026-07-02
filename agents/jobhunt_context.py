"""Weekly job-hunt context-pack refresh (Sprint 2, Task 6).

Distills the owner's ``candidate_profile.md`` (``jobhunt.roots.job_search``)
and ``goals.md`` (``jobhunt.roots.prep``) into a small always-on core block
(``jobhunt_context``) so every Hikari turn carries the pitch, target lanes,
verified-public repo list, and do-not-cite list without a tool call. The
block rides the existing always-on core_blocks injection (see
``agents.hooks._format_core_blocks``) — no further wiring is required for
the model to see it once written.

Both source files are owner-authored (trusted, unlike the other jobhunt
tools' data whose free-text fields flow through ``wrap_untrusted`` because
they can carry counterparty-facing content) — no untrusted wrapping here.
Still capped via ``jobhunt.prep_file_char_cap`` to bound prompt size.

Guard contract, never blanks the existing block:
  - ``jobhunt.enabled`` false -> return immediately (live kill switch —
    same first-line gate as ``interview_brief``/``daily_brief``; the
    scheduler-registration gate alone would not cover direct callers).
  - either source file missing/unreadable AND both empty after stripping
    -> no-op, log INFO (keeps whatever core_block value already exists).
  - distillation call raises, or returns empty / >1600 chars / missing the
    literal "NEVER CITE" heading -> keep the previous block, log WARNING.
  - structural verification, not just the heading: every name in cfg
    ``jobhunt.private_repo_names`` must appear in the NEVER CITE section
    (case-insensitive substring, mirroring ``tools/jobhunt/lint.py``'s
    convention), and none may appear in the PUBLIC REPOS OK TO CITE
    section -> either miss keeps the previous block, log WARNING.

Scheduling lives in ``agents/scheduler.py`` (weekly Monday 05:30 local,
plus a startup one-shot when the block is currently absent).
"""
from __future__ import annotations

import logging
from pathlib import Path

from agents import config as cfg
from agents.runtime import MODEL_HAIKU, run_internal_text
from storage import db

logger = logging.getLogger(__name__)

_LABEL = "jobhunt_context"
_MAX_OUTPUT_CHARS = 1600
_REQUIRED_HEADING = "NEVER CITE"
_PUBLIC_HEADING = "PUBLIC REPOS OK TO CITE"
_DISTILL_MAX_TOKENS = 800

# All five headings the distillation prompt mandates — used as section
# boundaries by _section(). Order here is documentation only; extraction
# finds the nearest following heading regardless of order.
_SECTION_HEADINGS = ("PITCH", "LANES", _PUBLIC_HEADING, _REQUIRED_HEADING, "NON-GOALS")

# Last-resort fallback when cfg ``jobhunt.private_repo_names`` is missing.
# Deliberately duplicated (not imported) from tools/jobhunt/lint.py's
# private ``_DEFAULT_PRIVATE_REPO_NAMES`` — mirrors that module's own
# precedent of duplicating a small local constant rather than coupling to
# another module's private API. Both mirror the real 2026-06-25
# do-not-cite list in candidate_profile.md.
_DEFAULT_PRIVATE_REPO_NAMES: tuple[str, ...] = (
    "NorMedBench", "fhir-safety-harness", "tg-bot-logger", "llm-social-agent",
)

_DISTILL_SYSTEM = (
    "You are a structured-output assistant distilling a job-hunt context "
    "pack for background injection into every future chat turn of an AI "
    "assistant. Output PLAIN TEXT only — no markdown fences, no prose "
    "outside the requested sections. Never invent facts absent from the "
    "source text you are given."
)


def _read_capped(path: Path, char_cap: int) -> str:
    try:
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8")[:char_cap]
    except OSError:
        logger.exception("jobhunt_context: failed to read %s", path)
        return ""


def _section(text: str, heading: str) -> str:
    """Content after the first occurrence of ``heading`` up to the nearest
    following known heading (or EOF). Empty string when the heading is
    absent. Heading match is case-sensitive (the guard requires the literal
    uppercase headings); callers lowercase the returned content for the
    case-insensitive repo-name checks."""
    idx = text.find(heading)
    if idx < 0:
        return ""
    start = idx + len(heading)
    end = len(text)
    for other in _SECTION_HEADINGS:
        if other == heading:
            continue
        j = text.find(other, start)
        if 0 <= j < end:
            end = j
    return text[start:end].strip()


def _private_repo_names() -> list[str]:
    """cfg ``jobhunt.private_repo_names`` with the hardcoded fallback —
    same resolution the lint uses when candidate_profile.md's do-not-cite
    section isn't available (this module verifies the *distilled* block, so
    the cfg list is the right independent reference, not the same source
    text the distillation already saw)."""
    raw = cfg.get("jobhunt.private_repo_names") or _DEFAULT_PRIVATE_REPO_NAMES
    return [str(n).strip() for n in raw if str(n).strip()]


def _build_prompt(profile_text: str, goals_text: str) -> str:
    return (
        "Distill the two source documents below into ONE plain-text block, "
        "target under 1200 characters total, with EXACTLY these five "
        "sections in this order, each starting with the EXACT heading text "
        "shown (including punctuation):\n\n"
        "PITCH: <two sentences — the candidate's core pitch>\n"
        "LANES: <one line — target role lanes>\n"
        "PUBLIC REPOS OK TO CITE: <comma-separated repo names only, no descriptions>\n"
        "NEVER CITE: <comma-separated repo names only — private repos that must "
        "never be cited as public>\n"
        "NON-GOALS: <one line — roles/paths explicitly out of scope>\n\n"
        "If a section has nothing to draw on in the source text, write "
        "'none' after its heading — never fabricate. Output ONLY the "
        "five-section block, nothing else (no preamble, no markdown "
        "fences).\n\n"
        "=== candidate_profile.md (core pitch, lanes, repo lists) ===\n"
        f"{profile_text}\n\n"
        "=== goals.md (target taxonomy, non-goals) ===\n"
        f"{goals_text}\n"
    )


async def refresh_jobhunt_context() -> None:
    """Distill candidate_profile.md + goals.md into the ``jobhunt_context``
    core_block. Called by the weekly scheduler job (and once at startup when
    the block is absent) — see ``agents/scheduler.py``. Never raises; every
    failure path keeps the previous block and logs instead."""
    if not bool(cfg.get("jobhunt.enabled", True)):
        return

    job_search_root = cfg.get("jobhunt.roots.job_search")
    prep_root = cfg.get("jobhunt.roots.prep")
    char_cap = int(cfg.get("jobhunt.prep_file_char_cap", 4000))

    profile_text = (
        _read_capped(Path(str(job_search_root)) / "candidate_profile.md", char_cap)
        if job_search_root else ""
    )
    goals_text = (
        _read_capped(Path(str(prep_root)) / "goals.md", char_cap)
        if prep_root else ""
    )

    if not profile_text.strip() and not goals_text.strip():
        logger.info(
            "jobhunt_context: candidate_profile.md and goals.md both "
            "missing/empty — no-op, keeping existing block"
        )
        return

    prompt = _build_prompt(profile_text, goals_text)
    try:
        raw = await run_internal_text(
            prompt, system=_DISTILL_SYSTEM, model=MODEL_HAIKU,
            max_tokens=_DISTILL_MAX_TOKENS,
        )
    except Exception:
        logger.exception(
            "jobhunt_context: distillation call raised — keeping previous block"
        )
        return

    text = (raw or "").strip()
    if not text:
        logger.warning(
            "jobhunt_context: distillation returned empty result — keeping "
            "previous block"
        )
        return
    if len(text) > _MAX_OUTPUT_CHARS:
        logger.warning(
            "jobhunt_context: distillation result too long (%d > %d chars) "
            "— keeping previous block",
            len(text), _MAX_OUTPUT_CHARS,
        )
        return
    if _REQUIRED_HEADING not in text:
        logger.warning(
            "jobhunt_context: distillation result missing %r heading — "
            "keeping previous block",
            _REQUIRED_HEADING,
        )
        return

    # Structural verification (fix pass 1): the heading alone doesn't prove
    # the do-not-cite list survived distillation. Every configured private
    # repo must be named in the NEVER CITE section, and none may leak into
    # the PUBLIC REPOS OK TO CITE section — that leak is the exact failure
    # mode the block exists to prevent.
    private_names = _private_repo_names()
    never_cite_lower = _section(text, _REQUIRED_HEADING).lower()
    missing_names = [n for n in private_names if n.lower() not in never_cite_lower]
    if missing_names:
        logger.warning(
            "jobhunt_context: distillation result's NEVER CITE section is "
            "missing private repo(s) %s — keeping previous block",
            missing_names,
        )
        return
    public_lower = _section(text, _PUBLIC_HEADING).lower()
    leaked_names = [n for n in private_names if n.lower() in public_lower]
    if leaked_names:
        logger.warning(
            "jobhunt_context: private repo(s) %s listed under PUBLIC REPOS "
            "OK TO CITE — keeping previous block",
            leaked_names,
        )
        return

    db.upsert_core_block(_LABEL, text)
    logger.info("jobhunt_context: refreshed core_block (%d chars)", len(text))
