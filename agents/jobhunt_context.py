"""Weekly job-hunt context-pack refresh (Sprint 2, Task 6).

Distills the owner's ``candidate_profile.md`` (``jobhunt.roots.job_search``)
and ``goals.md`` (``jobhunt.roots.prep``) into a small always-on core block
(``jobhunt_context``) so every Hikari turn carries the pitch, target lanes,
verified-public repo list, and do-not-cite list without a tool call. The
block rides the existing always-on core_blocks injection (see
``agents.hooks._format_core_blocks``) — no further wiring is required for
the model to see it once written.

The safety-critical NEVER CITE section is DETERMINISTIC (fix pass 2): the
LLM distills only PITCH / LANES / PUBLIC REPOS OK TO CITE / NON-GOALS, and
Python appends ``NEVER CITE: <cfg jobhunt.private_repo_names>`` afterwards —
the same authoritative list the touch-email lint uses. The LLM is never
trusted to reproduce the do-not-cite list (fix pass 1 tripped live on
exactly that: a truncated source produced "NEVER CITE: none" and the guard
kept a nonexistent old block forever).

Both source files are owner-authored (trusted, unlike the other jobhunt
tools' data whose free-text fields flow through ``wrap_untrusted`` because
they can carry counterparty-facing content) — no untrusted wrapping here.
Reads are capped via the module-local ``jobhunt.context_source_char_cap``
(default 12000): the shared ``prep_file_char_cap`` (4000) truncated the
real ~6.8K-char candidate_profile.md mid-way through its verified-public
section, starving the distiller of exactly the content it needed.

Guard contract, never blanks the existing block:
  - ``jobhunt.enabled`` false -> return immediately (live kill switch —
    same first-line gate as ``interview_brief``/``daily_brief``; the
    scheduler-registration gate alone would not cover direct callers).
  - either source file missing/unreadable AND both empty after stripping
    -> no-op, log INFO (keeps whatever core_block value already exists).
  - distillation call raises, or returns empty -> keep previous block,
    log WARNING.
  - a cfg private-repo name under the LLM's PUBLIC REPOS OK TO CITE
    section (case-insensitive substring, mirroring
    ``tools/jobhunt/lint.py``'s convention) -> keep previous block, log
    WARNING — a private repo presented as public is the exact failure mode
    the block exists to prevent.
  - a model-emitted NEVER CITE section is excised before assembly (the
    section is Python's job now); if nothing remains after excision ->
    keep previous block, log WARNING.
  - final ASSEMBLED block (LLM part + appended NEVER CITE) > 1600 chars ->
    keep previous block, log WARNING.
  - belt-and-braces on the assembled block (verifying a string Python
    itself wrote — cheap): literal "NEVER CITE" heading present, and every
    cfg private-repo name in its section.

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
_DEFAULT_SOURCE_CHAR_CAP = 12000

# All five headings the final block carries (four LLM-produced + the
# deterministic NEVER CITE) — used as section boundaries by _section() /
# _excise_section(). Order here is documentation only; extraction finds
# the nearest following heading regardless of order.
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
    absent. Heading match is case-sensitive (the block format mandates the
    literal uppercase headings); callers lowercase the returned content for
    the case-insensitive repo-name checks."""
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


def _excise_section(text: str, heading: str) -> str:
    """Remove the first occurrence of ``heading`` and its section content
    (same boundary rule as ``_section``). Returns ``text`` unchanged when
    the heading is absent."""
    idx = text.find(heading)
    if idx < 0:
        return text
    end = len(text)
    for other in _SECTION_HEADINGS:
        if other == heading:
            continue
        j = text.find(other, idx + len(heading))
        if 0 <= j < end:
            end = j
    return (text[:idx] + text[end:]).strip()


def _private_repo_names() -> list[str]:
    """cfg ``jobhunt.private_repo_names`` with the hardcoded fallback — the
    authoritative do-not-cite list, same source the touch-email lint falls
    back to. This module builds the block's NEVER CITE section from it
    directly (deterministic), never from LLM output."""
    raw = cfg.get("jobhunt.private_repo_names") or _DEFAULT_PRIVATE_REPO_NAMES
    return [str(n).strip() for n in raw if str(n).strip()]


def _build_prompt(profile_text: str, goals_text: str) -> str:
    return (
        "Distill the two source documents below into ONE plain-text block, "
        "target under 1200 characters total, with EXACTLY these four "
        "sections in this order, each starting with the EXACT heading text "
        "shown (including punctuation):\n\n"
        "PITCH: <two sentences — the candidate's core pitch>\n"
        "LANES: <one line — target role lanes>\n"
        "PUBLIC REPOS OK TO CITE: <comma-separated repo names only, no "
        "descriptions — ONLY repos the source text explicitly marks as "
        "verified public; never a repo the source text calls private>\n"
        "NON-GOALS: <one line — roles/paths explicitly out of scope>\n\n"
        "Do NOT output a NEVER CITE section — the private-repo do-not-cite "
        "list is appended deterministically by the caller, never by you.\n"
        "If a section has nothing to draw on in the source text, write "
        "'none' after its heading — never fabricate. Output ONLY the "
        "four-section block, nothing else (no preamble, no markdown "
        "fences).\n\n"
        "=== candidate_profile.md (core pitch, lanes, public repo list) ===\n"
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
    char_cap = int(
        cfg.get("jobhunt.context_source_char_cap", _DEFAULT_SOURCE_CHAR_CAP)
    )

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

    private_names = _private_repo_names()

    # Leak check on the LLM part: a private repo presented as public is the
    # exact failure mode the block exists to prevent.
    public_lower = _section(text, _PUBLIC_HEADING).lower()
    leaked_names = [n for n in private_names if n.lower() in public_lower]
    if leaked_names:
        logger.warning(
            "jobhunt_context: private repo(s) %s listed under PUBLIC REPOS "
            "OK TO CITE — keeping previous block",
            leaked_names,
        )
        return

    # NEVER CITE is Python's job — excise any model-emitted section so the
    # deterministic one appended below is the block's only NEVER CITE.
    if _REQUIRED_HEADING in text:
        logger.warning(
            "jobhunt_context: model emitted its own NEVER CITE section "
            "despite the prompt — excising it"
        )
        text = _excise_section(text, _REQUIRED_HEADING)
        if not text:
            logger.warning(
                "jobhunt_context: nothing left after excising the "
                "model-emitted NEVER CITE section — keeping previous block"
            )
            return

    final = f"{text}\n{_REQUIRED_HEADING}: " + ", ".join(private_names)

    if len(final) > _MAX_OUTPUT_CHARS:
        logger.warning(
            "jobhunt_context: assembled block too long (%d > %d chars) — "
            "keeping previous block",
            len(final), _MAX_OUTPUT_CHARS,
        )
        return

    # Belt-and-braces on the assembled block — verifying a string Python
    # itself just wrote (cheap, and future-proofs against assembly edits).
    if _REQUIRED_HEADING not in final:
        logger.warning(
            "jobhunt_context: assembled block missing %r heading — keeping "
            "previous block",
            _REQUIRED_HEADING,
        )
        return
    never_cite_lower = _section(final, _REQUIRED_HEADING).lower()
    missing_names = [n for n in private_names if n.lower() not in never_cite_lower]
    if missing_names:
        logger.warning(
            "jobhunt_context: assembled block's NEVER CITE section is "
            "missing private repo(s) %s — keeping previous block",
            missing_names,
        )
        return

    db.upsert_core_block(_LABEL, final)
    logger.info("jobhunt_context: refreshed core_block (%d chars)", len(final))
