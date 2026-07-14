"""Source-aware job-hunt context-pack refresh (Sprint 2, Task 6).

Distills the owner's ``candidate_profile.md`` and canonical ``DECISIONS.md``
(``jobhunt.roots.job_search``), plus ``goals.md`` (``jobhunt.roots.prep``),
into a small always-on core block
(``jobhunt_context``) so every Hikari turn carries the pitch, target lanes,
verified-public repo list, and do-not-cite list without a tool call. The
block rides the existing always-on core_blocks injection (see
``agents.hooks._format_core_blocks``) — no further wiring is required for
the model to see it once written.

The safety-critical sections are DETERMINISTIC. Python owns the current
ACTIVE / OPPORTUNISTIC / blocked taxonomy and ``NEVER CITE`` list; the model
may propose those sections but contradictions fail closed and accepted output
is rebuilt from Python's source-validated taxonomy. The model remains useful
only for PITCH and PUBLIC REPOS OK TO CITE. This also preserves the fix-pass-2
rule that the model is never trusted to reproduce the do-not-cite list.

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
  - source files missing/unreadable AND all empty after stripping
    -> no-op, log INFO (keeps whatever core_block value already exists).
  - distillation call raises, or returns empty -> keep previous block,
    log WARNING.
  - a cfg private-repo name under the LLM's PUBLIC REPOS OK TO CITE
    section (case-insensitive substring, mirroring
    ``tools/jobhunt/lint.py``'s convention) -> keep previous block, log
    WARNING — a private repo presented as public is the exact failure mode
    the block exists to prevent.
  - canonical goals/decision anchors missing, or model LANES/NON-GOALS
    contradicting the current taxonomy -> keep previous block, log WARNING.
  - a model-emitted NEVER CITE section is excised before assembly (the
    section is Python's job now); if nothing remains after excision ->
    keep previous block, log WARNING.
  - final ASSEMBLED block (LLM part + appended NEVER CITE) > 1600 chars ->
    keep previous block, log WARNING.
  - belt-and-braces on the assembled block (verifying a string Python
    itself wrote — cheap): literal "NEVER CITE" heading present, and every
    cfg private-repo name in its section.

Scheduling lives in ``agents/scheduler.py``.  A cheap polling job compares a
SHA-256 fingerprint of the trusted source files and refreshes immediately when
any changes; an age ceiling forces a daily fallback refresh even when their
content is unchanged.  The generated block carries the exact source hashes and
UTC refresh time so a chat never presents an undated operational snapshot.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agents import config as cfg
from agents.runtime import MODEL_HAIKU, run_internal_text
from storage import db

logger = logging.getLogger(__name__)

_LABEL = "jobhunt_context"
_MAX_OUTPUT_CHARS = 1600
_REQUIRED_HEADING = "NEVER CITE"
_PUBLIC_HEADING = "PUBLIC REPOS OK TO CITE"
_SOURCE_HEADING = "SOURCE SNAPSHOT"
_DISTILL_MAX_TOKENS = 800
_DEFAULT_SOURCE_CHAR_CAP = 12000
_DEFAULT_REFRESH_MAX_AGE_HOURS = 24
_RUNTIME_FINGERPRINT_KEY = "jobhunt_context_source_fingerprint"
_RUNTIME_REFRESHED_AT_KEY = "jobhunt_context_refreshed_at"
_TAXONOMY_VERSION = "2026-07-14-v2"

# All six headings the final block carries (four LLM-produced + deterministic
# NEVER CITE and SOURCE SNAPSHOT) — used as section boundaries by _section() /
# _excise_section(). Order here is documentation only; extraction finds
# the nearest following heading regardless of order.
_SECTION_HEADINGS = (
    "PITCH", "LANES", _PUBLIC_HEADING, _REQUIRED_HEADING, "NON-GOALS",
    _SOURCE_HEADING,
)

# DECISIONS.md is a long append-only log.  These headings identify the current
# role-target contract without sending the entire 39K file to the distiller.
# The complete file is still hashed, so every edit wakes the refresh loop.
_CURRENT_DECISION_MARKERS = (
    "offentlige forvaltnings-/stabsroller er blokkert",
    "farma er blokkert fullstendig",
    "ny målstack",
    "junior-tech-unntak",
    "miljøterapeut myk-setting-gate",
    "kommunale «rådgiver",
    "medisinskfaglig rådgiver",
    "senior-titler er blokkert",
)


@dataclass(frozen=True)
class TargetTaxonomy:
    active: str
    opportunistic: str
    blocked: str

    @property
    def lanes(self) -> str:
        return f"ACTIVE: {self.active} | OPPORTUNISTIC: {self.opportunistic}"


_CURRENT_TAXONOMY = TargetTaxonomy(
    active=(
        "hands-on e-health systems/implementation; private healthtech/digital-"
        "medicine delivery; junior tech (health IT first); soft-setting "
        "miljøterapeut (gated)"
    ),
    opportunistic=(
        "research/study coordination; register coordination (>=80%); coding/DRG"
    ),
    blocked=(
        "doctor/LIS; quality/patient-safety as an active lane; public-health "
        "administration/staff; pharma/CRO/MSL/medical-advisor; senior/lead/"
        "principal/chief roles; hard-setting miljøterapeut"
    ),
)

# Fail closed if the owner-authored canonical summary no longer contains the
# evidence behind the deterministic taxonomy. This makes source drift visible
# instead of silently continuing with code-era assumptions.
_GOALS_TAXONOMY_ANCHORS = (
    "hands-on e-health systems / implementation",
    "private healthtech / digital medicine delivery",
    "junior tech, preferably health it",
    "opportunistic medical-master bridge roles",
    "miljøterapeut only in a soft setting",
    "not public-health administration/staff functions",
    "quality/patient-safety/improvement is no longer an active lane",
    "not pharma/cro",
    "not a lege/lis role",
    "senior/lead/principal/chief titles",
)
_DECISIONS_TAXONOMY_ANCHORS = (
    "ny målstack",
    "offentlige forvaltnings-/stabsroller er blokkert",
    "farma er blokkert fullstendig",
    "miljøterapeut myk-setting-gate",
)

# Generated context is static positioning, never runtime telemetry.  These
# patterns are intentionally evaluated in Python after the model returns so a
# prompt instruction alone cannot leak historical arming/health prose from the
# append-only decision log into the always-on block.
_OPERATIONAL_OUTPUT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("armed", re.compile(r"\b(?:re[- ]?)?(?:armed|armert)\b", re.IGNORECASE)),
    ("dry_run", re.compile(r"\bdry[_ -]?run\b", re.IGNORECASE)),
    ("enabled_flag", re.compile(r"\b(?:enabled|disabled)\b", re.IGNORECASE)),
    (
        "last_run",
        re.compile(
            r"\b(?:last|latest|siste)\s+(?:scan|run|kjøring|søk|health|helse)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "runtime_health",
        re.compile(
            r"\b(?:scan|run|pipeline|source|kilde|health|helse)\b.{0,24}"
            r"\b(?:healthy|unhealthy|ok|failed|feilet|green|red|grønn|rød)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "operational_count",
        re.compile(
            r"(?:\b(?:count|counts|antall)\s*[:=]|\b\d+\s+"
            r"(?:emails?|messages?|threads?|actions?|leads?|arkivert|"
            r"archived|failed|pending|delivered)\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "delivery_status",
        re.compile(r"\b(?:delivery|leverings)[_ -]?status\b", re.IGNORECASE),
    ),
)

_BLOCKED_LANE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("quality", re.compile(r"\b(?:quality|kvalitet)\b", re.IGNORECASE)),
    (
        "patient_safety",
        re.compile(r"\b(?:patient[ -]?safety|pasientsikker\w*)\b", re.IGNORECASE),
    ),
    (
        "public_administration",
        re.compile(
            r"\b(?:public[ -]?health administration|offentlig\w* "
            r"(?:helse)?forvalt\w*|forvaltningsrådgiver)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "pharma_medical_advisor",
        re.compile(
            r"\b(?:pharma|farma|cro|cra|msl|medical[ -]?advisor|"
            r"medisinskfaglig rådgiver)\b",
            re.IGNORECASE,
        ),
    ),
)
_BRIDGE_LANE_PATTERN = re.compile(
    r"\b(?:helsedata|health[ -]?data|research|forskning|register(?:koord\w*)?|"
    r"study coordination|studiekoord\w*|coding|kodekonsulent|drg)\b",
    re.IGNORECASE,
)
_ACTIVE_AS_NONGOAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("e_health", re.compile(r"\b(?:e-?health|e-?helse)\b", re.IGNORECASE)),
    ("healthtech", re.compile(r"\b(?:healthtech|helsetek)\w*\b", re.IGNORECASE)),
    ("junior_tech", re.compile(r"\bjunior[ -]?(?:tech|it)\b", re.IGNORECASE)),
)

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
    "source text you are given. Never emit operational pipeline status such "
    "as enabled flags, dry-run state, last-run health, counts, or delivery "
    "state; those facts must be queried live."
)


def _read_capped_with_digest(path: Path, char_cap: int) -> tuple[str, str]:
    """Return capped prompt text plus a digest of the complete source file.

    Hashing the complete file means an edit after the prompt cap still wakes the
    refresh loop.  Only the capped text is ever sent to the distiller.
    """
    try:
        if not path.is_file():
            return "", "missing"
        text = path.read_text(encoding="utf-8")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return text[:char_cap], digest
    except (OSError, UnicodeError):
        logger.exception("jobhunt_context: failed to read %s", path)
        return "", "unreadable"


def _read_decisions_capped_with_digest(path: Path, char_cap: int) -> tuple[str, str]:
    """Return current target-decision bullets plus a complete-file digest.

    The decision log is append-only and much larger than the prompt budget.
    Relevant current contract bullets are selected deterministically. Arbitrary
    recent tail content is never included: the append-only log also contains
    historical runtime state that is unsafe to inject as current truth.
    """
    try:
        if not path.is_file():
            return "", "missing"
        text = path.read_text(encoding="utf-8")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        selected = [
            line for line in text.splitlines()
            if any(marker in line.lower() for marker in _CURRENT_DECISION_MARKERS)
        ]
        current = "\n".join(selected).strip()
        return current[:char_cap], digest
    except (OSError, UnicodeError):
        logger.exception("jobhunt_context: failed to read %s", path)
        return "", "unreadable"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _combined_fingerprint(
    profile_digest: str, decisions_digest: str, goals_digest: str
) -> str:
    raw = (
        f"taxonomy:{_TAXONOMY_VERSION}\n"
        f"candidate_profile.md:{profile_digest}\n"
        f"DECISIONS.md:{decisions_digest}\n"
        f"goals.md:{goals_digest}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _refresh_due(fingerprint: str, *, force: bool = False) -> bool:
    """True when the block is absent, sources changed, or the daily fallback is due."""
    if force or db.get_core_block(_LABEL) is None:
        return True
    if db.runtime_get(_RUNTIME_FINGERPRINT_KEY) != fingerprint:
        return True
    refreshed_raw = db.runtime_get(_RUNTIME_REFRESHED_AT_KEY)
    if not refreshed_raw:
        return True
    try:
        refreshed = datetime.fromisoformat(refreshed_raw)
        if refreshed.tzinfo is None:
            refreshed = refreshed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return True
    max_age = max(
        1,
        int(cfg.get(
            "jobhunt.context_refresh_max_age_hours",
            _DEFAULT_REFRESH_MAX_AGE_HOURS,
        )),
    )
    return _utc_now() - refreshed >= timedelta(hours=max_age)


def _operational_status_violation(text: str) -> str | None:
    """Return the matched fail-closed rule name, without logging source text."""
    for name, pattern in _OPERATIONAL_OUTPUT_PATTERNS:
        if pattern.search(text):
            return name
    return None


def _derive_target_taxonomy(
    decisions_text: str, goals_text: str
) -> TargetTaxonomy | None:
    """Validate current canonical sources before returning the fixed taxonomy."""
    goals_lower = goals_text.lower()
    decisions_lower = decisions_text.lower()
    missing_goals = [a for a in _GOALS_TAXONOMY_ANCHORS if a not in goals_lower]
    missing_decisions = [
        a for a in _DECISIONS_TAXONOMY_ANCHORS if a not in decisions_lower
    ]
    if missing_goals or missing_decisions:
        logger.warning(
            "jobhunt_context: canonical taxonomy anchors missing "
            "(goals=%d, decisions=%d) — keeping previous block",
            len(missing_goals), len(missing_decisions),
        )
        return None
    return _CURRENT_TAXONOMY


def _section_value(text: str, heading: str) -> str:
    return _section(text, heading).lstrip(" :\t").strip()


def _model_taxonomy_violation(text: str) -> str | None:
    """Reject explicit stale/contradictory model taxonomy before assembly."""
    lanes = _section_value(text, "LANES")
    non_goals = _section_value(text, "NON-GOALS")
    if not lanes:
        return "missing_lanes"
    if not non_goals:
        return "missing_non_goals"

    for name, pattern in _BLOCKED_LANE_PATTERNS:
        if pattern.search(lanes):
            return f"blocked_active_lane:{name}"

    # Every unqualified LANES item is primary. With the requested structured
    # form, only inspect the ACTIVE segment; bridge roles belong under the
    # explicit OPPORTUNISTIC segment.
    upper = lanes.upper()
    if "ACTIVE:" in upper and "OPPORTUNISTIC:" in upper:
        active_start = upper.index("ACTIVE:") + len("ACTIVE:")
        opportunistic_start = upper.index("OPPORTUNISTIC:")
        active_segment = lanes[active_start:opportunistic_start]
    else:
        active_segment = lanes
    if _BRIDGE_LANE_PATTERN.search(active_segment):
        return "bridge_presented_as_primary"

    for name, pattern in _ACTIVE_AS_NONGOAL_PATTERNS:
        if pattern.search(non_goals):
            return f"active_lane_blocked:{name}"
    return None


def _assemble_static_context(text: str, taxonomy: TargetTaxonomy) -> str | None:
    """Keep model pitch/public evidence; own taxonomy is deterministic."""
    pitch = _section_value(text, "PITCH")
    public = _section_value(text, _PUBLIC_HEADING)
    if not pitch or not public:
        return None
    return (
        f"PITCH: {pitch}\n"
        f"LANES: {taxonomy.lanes}\n"
        f"{_PUBLIC_HEADING}: {public}\n"
        f"NON-GOALS: {taxonomy.blocked}"
    )


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


def _build_prompt(
    profile_text: str,
    decisions_text: str,
    goals_text: str,
    taxonomy: TargetTaxonomy,
) -> str:
    return (
        "Distill the three source documents below into ONE plain-text block, "
        "target under 1200 characters total, with EXACTLY these four "
        "sections in this order, each starting with the EXACT heading text "
        "shown (including punctuation):\n\n"
        "PITCH: <two sentences — the candidate's core pitch>\n"
        f"LANES: {taxonomy.lanes}\n"
        "PUBLIC REPOS OK TO CITE: <comma-separated repo names only, no "
        "descriptions — ONLY repos the source text explicitly marks as "
        "verified public; never a repo the source text calls private>\n"
        f"NON-GOALS: {taxonomy.blocked}\n\n"
        "Copy the LANES and NON-GOALS values above exactly. Python validates "
        "them and deterministically owns the final taxonomy.\n"
        "Do NOT output a NEVER CITE section — the private-repo do-not-cite "
        "list is appended deterministically by the caller, never by you.\n"
        "Use DECISIONS.md only for current target/non-target constraints. "
        "Its operational history is not live state and must never appear in "
        "the output.\n"
        "If PITCH or PUBLIC REPOS has nothing to draw on in the source text, "
        "write 'none' after its heading — never fabricate. Output ONLY the "
        "four-section block, nothing else (no preamble, no markdown "
        "fences).\n\n"
        "=== candidate_profile.md (core pitch, lanes, public repo list) ===\n"
        f"{profile_text}\n\n"
        "=== DECISIONS.md (canonical current target constraints) ===\n"
        f"{decisions_text}\n\n"
        "=== goals.md (target taxonomy, non-goals) ===\n"
        f"{goals_text}\n"
    )


async def refresh_jobhunt_context(*, force: bool = False) -> bool:
    """Distill candidate_profile.md + DECISIONS.md + goals.md into the ``jobhunt_context``
    core_block. The scheduler polls source fingerprints and enforces a daily
    fallback — see ``agents/scheduler.py``. Never raises; every failure path
    keeps the previous block and returns ``False``."""
    if not bool(cfg.get("jobhunt.enabled", True)):
        return False

    job_search_root = cfg.get("jobhunt.roots.job_search")
    prep_root = cfg.get("jobhunt.roots.prep")
    char_cap = int(
        cfg.get("jobhunt.context_source_char_cap", _DEFAULT_SOURCE_CHAR_CAP)
    )

    profile_text, profile_digest = (
        _read_capped_with_digest(
            Path(str(job_search_root)) / "candidate_profile.md", char_cap
        )
        if job_search_root else ("", "unconfigured")
    )
    decisions_text, decisions_digest = (
        _read_decisions_capped_with_digest(
            Path(str(job_search_root)) / "DECISIONS.md", char_cap
        )
        if job_search_root else ("", "unconfigured")
    )
    goals_text, goals_digest = (
        _read_capped_with_digest(Path(str(prep_root)) / "goals.md", char_cap)
        if prep_root else ("", "unconfigured")
    )
    fingerprint = _combined_fingerprint(
        profile_digest, decisions_digest, goals_digest
    )

    if (
        not profile_text.strip()
        and not decisions_text.strip()
        and not goals_text.strip()
    ):
        logger.info(
            "jobhunt_context: candidate_profile.md, DECISIONS.md, and goals.md all "
            "missing/empty — no-op, keeping existing block"
        )
        return False

    if not _refresh_due(fingerprint, force=force):
        logger.debug("jobhunt_context: source fingerprint unchanged and snapshot fresh")
        return False

    taxonomy = _derive_target_taxonomy(decisions_text, goals_text)
    if taxonomy is None:
        return False

    prompt = _build_prompt(profile_text, decisions_text, goals_text, taxonomy)
    try:
        raw = await run_internal_text(
            prompt, system=_DISTILL_SYSTEM, model=MODEL_HAIKU,
            max_tokens=_DISTILL_MAX_TOKENS,
        )
    except Exception:
        logger.exception(
            "jobhunt_context: distillation call raised — keeping previous block"
        )
        return False

    text = (raw or "").strip()
    if not text:
        logger.warning(
            "jobhunt_context: distillation returned empty result — keeping "
            "previous block"
        )
        return False

    operational_violation = _operational_status_violation(text)
    if operational_violation:
        logger.warning(
            "jobhunt_context: generated block contains forbidden operational "
            "status (%s) — keeping previous block",
            operational_violation,
        )
        return False

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
        return False

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
            return False

    taxonomy_violation = _model_taxonomy_violation(text)
    if taxonomy_violation:
        logger.warning(
            "jobhunt_context: generated taxonomy conflicts with canonical "
            "targets (%s) — keeping previous block",
            taxonomy_violation,
        )
        return False

    static_context = _assemble_static_context(text, taxonomy)
    if static_context is None:
        logger.warning(
            "jobhunt_context: generated block missing pitch/public evidence — "
            "keeping previous block"
        )
        return False
    text = static_context

    refreshed_at = _utc_now().replace(microsecond=0).isoformat()
    source_snapshot = (
        f"{_SOURCE_HEADING}: taxonomy:{_TAXONOMY_VERSION}; "
        f"candidate_profile.md sha256:{profile_digest[:16]}; "
        f"DECISIONS.md sha256:{decisions_digest[:16]}; "
        f"goals.md sha256:{goals_digest[:16]}; refreshed_utc:{refreshed_at}"
    )
    final = (
        f"{text}\n{_REQUIRED_HEADING}: " + ", ".join(private_names)
        + f"\n{source_snapshot}"
    )

    if len(final) > _MAX_OUTPUT_CHARS:
        logger.warning(
            "jobhunt_context: assembled block too long (%d > %d chars) — "
            "keeping previous block",
            len(final), _MAX_OUTPUT_CHARS,
        )
        return False

    # Belt-and-braces on the assembled block — verifying a string Python
    # itself just wrote (cheap, and future-proofs against assembly edits).
    if _REQUIRED_HEADING not in final:
        logger.warning(
            "jobhunt_context: assembled block missing %r heading — keeping "
            "previous block",
            _REQUIRED_HEADING,
        )
        return False
    never_cite_lower = _section(final, _REQUIRED_HEADING).lower()
    missing_names = [n for n in private_names if n.lower() not in never_cite_lower]
    if missing_names:
        logger.warning(
            "jobhunt_context: assembled block's NEVER CITE section is "
            "missing private repo(s) %s — keeping previous block",
            missing_names,
        )
        return False

    try:
        db.upsert_core_block_snapshot(
            _LABEL,
            final,
            {
                _RUNTIME_FINGERPRINT_KEY: fingerprint,
                _RUNTIME_REFRESHED_AT_KEY: refreshed_at,
            },
        )
    except Exception:
        logger.exception(
            "jobhunt_context: failed to persist snapshot metadata; retrying next poll"
        )
        return False
    logger.info("jobhunt_context: refreshed core_block (%d chars)", len(final))
    return True
