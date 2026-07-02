"""Deterministic language-rails lint for job-hunt touch-email drafts
(Sprint 2, Task 4).

``check(text)`` is a pure regex/substring scan — no LLM judgment anywhere
in this module. That's the point: the rails can't be argued with, drift
with model mood, or get talked past by a persuasive recompose. ANY hit
blocks a draft (see ``tools/jobhunt/drafter.py``'s pipeline: compose ->
check() -> on any hit, ONE recompose naming the hits -> check() again ->
still failing = refuse, never call the Gmail draft-create MCP tool).

Patterns (case-insensitive where sensible — see inline comments for the
two that are deliberately NOT anchored/insensitive):
  - ``;``                                             semicolon anywhere
  - ``B2\\+``                                          claims B2+ (bare "B2" is fine)
  - ``flyktning``                                      refugee status must never appear
  - ``\\bvisum|\\bvisa|oppholdstillatelse|immigration``  visa/immigration status
    (visum/visa left-bounded so 'avisa' doesn't false-positive)
  - ``\\b2027\\b``                                      the collective-protection date
  - ``\\bikkje\\b|\\bkorleis\\b|\\beg\\b``                nynorsk giveaways
  - each private-repo do-not-cite name                 case-insensitive substring

Private-repo names are resolved fresh on every ``check()`` call (never
cached) from ``<jobhunt.roots.job_search>/candidate_profile.md``'s
do-not-cite section — the file is the single source of truth the owner
edits when a repo's visibility changes; a cached lint would silently drift
stale. Falls back to cfg ``jobhunt.private_repo_names``, and further to a
hardcoded default, when the file/section is missing or unreadable — the
lint must never go silent just because a file moved.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from agents import config as cfg

logger = logging.getLogger(__name__)

# Deterministic, order-preserving pattern -> human-readable hit label.
# flyktning/oppholdstillatelse/immigration are deliberately NOT
# word-bounded — a substring hit is still a hit worth blocking on for a
# do-not-send rail, and no innocent Norwegian word contains them.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r";"), "semicolon in subject/body prose"),
    (re.compile(r"B2\+", re.IGNORECASE), "claims B2+ language level ('B2+')"),
    (re.compile(r"flyktning", re.IGNORECASE), "mentions 'flyktning' (refugee status)"),
    (
        # visum/visa are LEFT-word-bounded so bokmal 'avisa' (the
        # newspaper — a natural touch-1 new-angle word) never
        # false-positives, while compounds like 'visasøknad' still hit —
        # see test_avisa_does_not_trigger_visa_rule /
        # test_visasoknad_still_caught.
        re.compile(r"\bvisum|\bvisa|oppholdstillatelse|immigration", re.IGNORECASE),
        "mentions visa/immigration status",
    ),
    (re.compile(r"\b2027\b"), "mentions the year 2027 (collective-protection date)"),
    (re.compile(r"\bikkje\b", re.IGNORECASE), "nynorsk giveaway: 'ikkje'"),
    (re.compile(r"\bkorleis\b", re.IGNORECASE), "nynorsk giveaway: 'korleis'"),
    # Word-bounded on both sides so bokmal "jeg" (containing "eg" but with
    # no boundary before it) never false-positives — see
    # test_jeg_does_not_trigger_eg_rule.
    (re.compile(r"\beg\b", re.IGNORECASE), "nynorsk giveaway: 'eg'"),
)

# Last-resort fallback when neither candidate_profile.md's do-not-cite
# section nor cfg jobhunt.private_repo_names is readable. Mirrors the
# real 2026-06-25 do-not-cite list (see candidate_profile.md) so a
# misconfigured/missing file never silently drops the rail entirely.
_DEFAULT_PRIVATE_REPO_NAMES: tuple[str, ...] = (
    "NorMedBench", "fhir-safety-harness", "tg-bot-logger", "llm-social-agent",
)


def _root(name: str) -> Path | None:
    raw = cfg.get(f"jobhunt.roots.{name}")
    if not raw:
        return None
    p = Path(str(raw))
    return p if p.is_dir() else None


def _section_after_heading(text: str, needle: str) -> str:
    """Body of the first '## ...' heading whose text contains `needle`
    (case-insensitive), up to the next '## ' heading or EOF. Empty string
    if no heading matches. Duplicated (not shared) with
    ``tools/jobhunt/drafter.py``'s own copy — mirrors
    ``tools/jobhunt/reply_radar.py``'s precedent of duplicating a small
    adapter-local helper rather than coupling two modules' private APIs."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("## ") and needle.lower() in stripped.lower():
            start = i + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start, len(lines)):
        if lines[j].strip().startswith("## "):
            end = j
            break
    return "\n".join(lines[start:end]).strip()


def private_repo_names() -> list[str]:
    """Do-not-cite private repo names for the current lint pass.

    Resolution order (first that yields a non-empty list wins):
      1. Parse the do-not-cite section of
         ``<jobhunt.roots.job_search>/candidate_profile.md`` — the text up
         to the first em dash in that section (the do-not-cite line is
         written as a backtick-quoted name list followed by an em-dash-led
         explanatory clause; stopping at the dash keeps later backticked
         tokens in the same paragraph — e.g. a skill name — out of the
         list).
      2. cfg ``jobhunt.private_repo_names``.
      3. The hardcoded default above.
    """
    cfg_fallback = cfg.get("jobhunt.private_repo_names")
    fallback = [str(n) for n in (cfg_fallback or _DEFAULT_PRIVATE_REPO_NAMES)]

    root = _root("job_search")
    if root is None:
        return fallback
    fp = root / "candidate_profile.md"
    if not fp.is_file():
        return fallback
    try:
        text = fp.read_text(encoding="utf-8")
    except Exception:
        logger.exception("jobhunt lint: failed to read candidate_profile.md at %s", fp)
        return fallback

    section = _section_after_heading(text, "private")
    if not section:
        return fallback
    prefix = section.split("—", 1)[0]
    names = [n.strip() for n in re.findall(r"`([^`]+)`", prefix) if n.strip()]
    return names or fallback


def check(text: str) -> list[str]:
    """Deterministic banned-pattern scan over composed email text (subject
    line + body together — callers pass the full composed text, not just
    the body). Returns human-readable hit descriptions; an empty list
    means the text is clean. ANY hit blocks the draft — see
    ``tools/jobhunt/drafter.py``."""
    text = text or ""
    hits: list[str] = []
    for pattern, label in _PATTERNS:
        if pattern.search(text):
            hits.append(label)

    lower_text = text.lower()
    for name in private_repo_names():
        name = (name or "").strip()
        if name and name.lower() in lower_text:
            hits.append(f"private repo name mentioned: {name!r}")

    return hits
