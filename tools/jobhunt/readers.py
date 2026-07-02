"""Read-only typed adapters over the owner's job-hunt repos.

HARD CONTRACT: read-only. Connections open with SQLite read-only URIs;
no function here may write to outreach.db / job_search.db / Notion —
both repos keep SQLite and Notion 1:1 and an external write desyncs them.
No LLM in this data path (typed-adapter provenance rule).

Every public function is pure, synchronous, and NEVER raises: any failure
(missing root, missing db file, corrupt db, malformed markdown) is caught,
logged, and mapped to the function's empty value (``[]``, ``{}``, ``None``,
or ``set()``).

Schemas mirrored (verified read-only against the real repos):
  - outreach.db table ``organisasjoner`` — all-TEXT columns except ``id``;
    dates are ``YYYY-MM-DD`` strings.
  - job_search.db table ``jobs`` — all-TEXT columns, several with spaces
    (``"Contact email"``, ``"Next action"``, ...) that MUST be quoted in SQL.
  - get_hired_prep/index.md — one markdown table, columns
    ``Company | Role | Tier | Stage | Interview date | Next step | Folder``.
  - get_hired_prep/stories/story_bank.md — ``### N. Title`` story blocks;
    a story is CONFIRMED iff its metadata line contains
    ``Confirmed: YYYY-MM-DD`` (the literal marker used by ``story-intake``,
    e.g. ``> Archetypes: tag · Confirmed: 2026-07-02 · Last-used: —``).
    Until that line exists the story is an unconfirmed anchor.

Key contract (dict keys returned by each reader):
  - outreach_due: org, gruppe, kontakt, epost, touch, due, days_overdue,
    varm_hook, notater_tail
  - application_deadlines: org, stilling, frist, next_action
  - interviews_upcoming: org, slug, date, source, prep_state
  - org_context: the RAW sqlite row (native outreach.db column names --
    ``organisasjon``, not ``org``) or ``{"ambiguous": [names]}`` when more
    than one row matches. Deliberately NOT adapted to the unified key below
    -- it's a raw-row lookup, and the raw column name signals that.
  - prep_files: any subset of company_dossier, positioning, interview_plan,
    tier, confirmed_stories, depending on what exists on disk
  - contact_emails: ``set[str]`` of lowercased email addresses (not a dict)
  - pipeline_summary: ``{"outreach": {status: count}, "applications":
    {status: count}}``

The employer/company-name key is unified to ``org`` across outreach_due,
application_deadlines, and interviews_upcoming (interviews_upcoming also
keeps its own ``slug`` and ``date`` keys). org_context is the sole
exception, per above.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from agents import config as cfg

logger = logging.getLogger(__name__)


def _root(name: str) -> Path | None:
    raw = cfg.get(f"jobhunt.roots.{name}")
    if not raw:
        return None
    p = Path(str(raw))
    return p if p.is_dir() else None


def _ro_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# Møte = warm relationship, cadence-exempt (never a re-touch candidate).
# outreach_due filters on status='Sendt' directly, which already excludes
# Møte/Død/Avslag/Blokkert rows — this tuple documents *why* a Møte row can
# never surface there (see the constraint test in test_jobhunt_readers.py).
_EXCLUDED_OUTREACH = ("Møte", "Død", "Avslag", "Blokkert")

# contact_emails() has its OWN (narrower) exclusion: Møte contacts are still
# real, current relationships — only dead/declined/blocked ones drop out.
_CONTACT_EMAIL_EXCLUDED_STATUSES = ("Død", "Avslag", "Blokkert")

# jobs.db statuses whose contact is still an active, addressable relationship.
_CONTACT_EMAIL_JOB_STATUSES = ("Applied", "Interview", "To apply")

_CONFIRMED_STORY_RE = re.compile(r"Confirmed:\s*\d{4}-\d{2}-\d{2}")

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def outreach_due(today: date) -> list[dict]:
    root = _root("outreach")
    if root is None or not (root / "outreach.db").exists():
        return []
    lo = (today - timedelta(days=int(cfg.get("jobhunt.overdue_grace_days", 14)))).isoformat()
    hi = (today + timedelta(days=int(cfg.get("jobhunt.touch_lookahead_days", 1)))).isoformat()
    tail = int(cfg.get("jobhunt.notater_tail_chars", 240))
    out: list[dict] = []
    try:
        excl_placeholders = ",".join("?" * len(_EXCLUDED_OUTREACH))
        with _ro_conn(root / "outreach.db") as conn:
            rows = conn.execute(
                "SELECT organisasjon, gruppe, kontaktperson, kontakt_epost, status,"
                "       oppfolging_dato, oppfolging2_dato, reengasjement_dato,"
                "       varm_hook, notater"
                "  FROM organisasjoner"
                " WHERE status = 'Sendt'"
                # Belt-and-braces: status='Sendt' already excludes
                # Møte/Død/Avslag/Blokkert, but this AND NOT IN (...) keeps
                # the Møte-never-surfaces invariant true even if the
                # status='Sendt' filter above is ever loosened/broadened.
                f"   AND status NOT IN ({excl_placeholders})",
                _EXCLUDED_OUTREACH,
            ).fetchall()
        for r in rows:
            for col, label in (("oppfolging_dato", "1"), ("oppfolging2_dato", "2"),
                               ("reengasjement_dato", "reengasjement")):
                d = (r[col] or "").strip()
                if d and lo <= d <= hi:
                    out.append({
                        "org": r["organisasjon"], "gruppe": r["gruppe"],
                        "kontakt": r["kontaktperson"], "epost": r["kontakt_epost"],
                        "touch": label, "due": d,
                        "days_overdue": (today - date.fromisoformat(d)).days,
                        "varm_hook": r["varm_hook"],
                        "notater_tail": (r["notater"] or "")[-tail:],
                    })
                    break   # one entry per row — earliest matching touch wins
    except Exception:
        logger.exception("jobhunt: outreach_due failed")
        return []
    # Dedup across DB ROWS that share organisasjon (e.g. NTNU IHA is tracked
    # under more than one gruppe row in the real data) — keep only the
    # earliest-due entry per org, not one per row.
    best_by_org: dict[str, dict] = {}
    for entry in out:
        org = entry["org"]
        if org not in best_by_org or entry["due"] < best_by_org[org]["due"]:
            best_by_org[org] = entry
    out = list(best_by_org.values())
    out.sort(key=lambda x: x["due"])
    return out


def pipeline_summary() -> dict:
    out: dict = {"outreach": {}, "applications": {}}

    root = _root("outreach")
    if root is not None and (root / "outreach.db").exists():
        try:
            with _ro_conn(root / "outreach.db") as conn:
                rows = conn.execute(
                    "SELECT status, COUNT(*) AS n FROM organisasjoner GROUP BY status"
                ).fetchall()
            out["outreach"] = {(r["status"] or "(none)"): r["n"] for r in rows}
        except Exception:
            logger.exception("jobhunt: pipeline_summary outreach failed")
            out["outreach"] = {}

    root2 = _root("job_search")
    if root2 is not None and (root2 / "job_search.db").exists():
        try:
            with _ro_conn(root2 / "job_search.db") as conn:
                rows = conn.execute(
                    'SELECT Status, COUNT(*) AS n FROM jobs GROUP BY Status'
                ).fetchall()
            out["applications"] = {(r["Status"] or "(none)"): r["n"] for r in rows}
        except Exception:
            logger.exception("jobhunt: pipeline_summary applications failed")
            out["applications"] = {}

    return out


def application_deadlines(today: date) -> list[dict]:
    root = _root("job_search")
    if root is None or not (root / "job_search.db").exists():
        return []
    window = int(cfg.get("jobhunt.deadline_window_days", 7))
    hi = (today + timedelta(days=window)).isoformat()
    out: list[dict] = []
    try:
        with _ro_conn(root / "job_search.db") as conn:
            rows = conn.execute(
                'SELECT Stilling, Arbeidsgiver, Soknadsfrist, "Next action"'
                '  FROM jobs'
                " WHERE Status = 'To apply'"
            ).fetchall()
        for r in rows:
            d = (r["Soknadsfrist"] or "").strip()
            if d and d <= hi:
                out.append({
                    "stilling": r["Stilling"],
                    "org": r["Arbeidsgiver"],
                    "frist": d,
                    "next_action": r["Next action"],
                })
    except Exception:
        logger.exception("jobhunt: application_deadlines failed")
        return []
    out.sort(key=lambda x: x["frist"])
    return out


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s or "unknown"


# Corporate suffixes stripped before fuzzy company-name matching so e.g.
# "DNB Bank ASA" and "DNB Bank ASA (Radical AI)" compare on their actual
# distinguishing words rather than being pulled apart by "ASA".
_CORP_STOPWORDS = {"asa", "as", "ab", "the"}


def _fuzzy_words(name: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    return {w for w in words if w not in _CORP_STOPWORDS}


def _fuzzy_company_match(jobs_name: str, index_name: str) -> bool:
    """True if a jobs.db Arbeidsgiver name and a get_hired_prep/index.md
    Company name refer to the same employer. Normalizes both to lowercase
    alphanumeric word sets with corporate stopwords dropped, then matches if
    one word set is a subset of the other, OR the jobs name (as a compact
    alphanumeric string) contains every word from the index name — covering
    cases where a name is written as one run-together token instead of
    space-separated words.

    Must merge the real-world triple: jobs "DNB Bank ASA (Radical AI)" vs
    index "DNB Bank ASA" vs slug dnb-radical-ai.
    """
    jobs_words = _fuzzy_words(jobs_name)
    index_words = _fuzzy_words(index_name)
    if not jobs_words or not index_words:
        return False
    if jobs_words <= index_words or index_words <= jobs_words:
        return True
    jobs_compact = "".join(re.findall(r"[a-z0-9]+", (jobs_name or "").lower()))
    return all(w in jobs_compact for w in index_words)


def _parse_index_md(path: Path) -> list[dict]:
    """Parse the single markdown table in get_hired_prep/index.md into row
    dicts keyed by header name. HTML comments (used in the real file to keep
    an "example row" template) are stripped before parsing so they never
    leak in as data rows."""
    text = _HTML_COMMENT_RE.sub("", path.read_text(encoding="utf-8"))
    header: list[str] | None = None
    rows: list[dict] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if header is None:
            header = cells
            continue
        if all(re.fullmatch(r":?-+:?", c) for c in cells):
            continue  # divider row
        if len(cells) != len(header):
            continue
        rows.append(dict(zip(header, cells)))
    return rows


def interviews_upcoming(today: date) -> list[dict]:
    # jobs.db has no reliable interview-date column. "Follow-up date" is a
    # banned-legacy field: it is provably wrong for interview dating (the
    # real DNB row carries Follow-up date 2026-07-10 for an interview that
    # actually happened 2026-06-29 per index.md). index.md is the ONLY
    # dated source for interviews — jobs-sourced rows always carry
    # date=None.
    jobs_entries: list[dict] = []
    prep_entries: list[dict] = []

    root = _root("job_search")
    if root is not None and (root / "job_search.db").exists():
        try:
            with _ro_conn(root / "job_search.db") as conn:
                rows = conn.execute(
                    "SELECT Arbeidsgiver FROM jobs WHERE Status = 'Interview'"
                ).fetchall()
            for r in rows:
                company = r["Arbeidsgiver"] or ""
                jobs_entries.append({
                    "org": company,
                    "slug": _slugify(company),
                    "date": None,
                    "source": "jobs",
                    "prep_state": None,
                })
        except Exception:
            logger.exception("jobhunt: interviews_upcoming jobs failed")

    prep_root = _root("prep")
    if prep_root is not None:
        index_fp = prep_root / "index.md"
        if index_fp.is_file():
            try:
                for row in _parse_index_md(index_fp):
                    raw_date = (row.get("Interview date") or "").strip()
                    d10 = raw_date[:10]
                    try:
                        parsed = date.fromisoformat(d10)
                    except ValueError:
                        continue
                    if parsed < today:
                        continue
                    company = (row.get("Company") or "").strip()
                    folder = (row.get("Folder") or "").strip().rstrip("/")
                    slug = folder.rsplit("/", 1)[-1] if folder else _slugify(company)
                    prep_entries.append({
                        "org": company,
                        "slug": slug or _slugify(company),
                        "date": d10,
                        "source": "prep",
                        "prep_state": row.get("Stage"),
                    })
            except Exception:
                logger.exception("jobhunt: interviews_upcoming prep failed")

    # Union-dedup: index-sourced entries (dated, prep_state-aware) always
    # win over jobs-sourced entries for the same employer. A jobs row that
    # fuzzy-matches an index row's company is dropped rather than emitted as
    # a second, undated duplicate.
    out = list(prep_entries)
    for job_entry in jobs_entries:
        if any(_fuzzy_company_match(job_entry["org"], p["org"]) for p in prep_entries):
            continue
        out.append(job_entry)

    out.sort(key=lambda x: (x["date"] is None, x["date"] or ""))
    return out


def org_context(name_or_fragment: str) -> dict | None:
    root = _root("outreach")
    if root is None or not (root / "outreach.db").exists():
        return None
    frag = (name_or_fragment or "").strip()
    if not frag:
        return None
    try:
        with _ro_conn(root / "outreach.db") as conn:
            rows = conn.execute(
                "SELECT * FROM organisasjoner WHERE organisasjon LIKE ?",
                (f"%{frag}%",),
            ).fetchall()
    except Exception:
        logger.exception("jobhunt: org_context failed")
        return None
    if not rows:
        return None
    if len(rows) > 1:
        return {"ambiguous": sorted(r["organisasjon"] for r in rows)}
    return dict(rows[0])


def _confirmed_stories(text: str) -> list[str]:
    blocks = re.split(r"(?=^### )", text, flags=re.MULTILINE)
    confirmed = []
    for block in blocks:
        if block.lstrip().startswith("### ") and _CONFIRMED_STORY_RE.search(block):
            confirmed.append(block.strip())
    return confirmed


def prep_files(slug: str) -> dict:
    root = _root("prep")
    if root is None:
        return {}
    slug = (slug or "").strip()
    if not slug:
        return {}
    company_dir = root / "companies" / slug
    if not company_dir.is_dir():
        return {}
    char_cap = int(cfg.get("jobhunt.prep_file_char_cap", 4000))
    out: dict = {}
    try:
        for key, fname in (
            ("company_dossier", "company_dossier.md"),
            ("positioning", "positioning.md"),
            ("interview_plan", "interview_plan.md"),
        ):
            fp = company_dir / fname
            if fp.is_file():
                out[key] = fp.read_text(encoding="utf-8")[:char_cap]

        log_fp = company_dir / "log.md"
        if log_fp.is_file():
            lines = log_fp.read_text(encoding="utf-8").splitlines()
            out["tier"] = lines[0] if lines else ""

        story_fp = root / "stories" / "story_bank.md"
        if story_fp.is_file():
            out["confirmed_stories"] = _confirmed_stories(
                story_fp.read_text(encoding="utf-8")
            )
    except Exception:
        logger.exception("jobhunt: prep_files failed for slug=%s", slug)
        return {}
    return out


def contact_emails() -> set[str]:
    emails: set[str] = set()

    root = _root("outreach")
    if root is not None and (root / "outreach.db").exists():
        try:
            placeholders = ",".join("?" * len(_CONTACT_EMAIL_EXCLUDED_STATUSES))
            with _ro_conn(root / "outreach.db") as conn:
                rows = conn.execute(
                    "SELECT kontakt_epost FROM organisasjoner"
                    f" WHERE status NOT IN ({placeholders})",
                    _CONTACT_EMAIL_EXCLUDED_STATUSES,
                ).fetchall()
            emails |= {
                r["kontakt_epost"].strip().lower()
                for r in rows if (r["kontakt_epost"] or "").strip()
            }
        except Exception:
            logger.exception("jobhunt: contact_emails outreach failed")

    root2 = _root("job_search")
    if root2 is not None and (root2 / "job_search.db").exists():
        try:
            placeholders = ",".join("?" * len(_CONTACT_EMAIL_JOB_STATUSES))
            with _ro_conn(root2 / "job_search.db") as conn:
                rows = conn.execute(
                    'SELECT "Contact email" FROM jobs'
                    f" WHERE Status IN ({placeholders})",
                    _CONTACT_EMAIL_JOB_STATUSES,
                ).fetchall()
            emails |= {
                r["Contact email"].strip().lower()
                for r in rows if (r["Contact email"] or "").strip()
            }
        except Exception:
            logger.exception("jobhunt: contact_emails jobs failed")

    return emails
