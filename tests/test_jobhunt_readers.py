"""Tests for tools/jobhunt/readers.py — read-only typed adapters over the
owner's outreach / job-search / get_hired_prep repos.

Fixtures build THROWAWAY sqlite DBs + markdown trees under tmp_path that
mirror the REAL production schemas (verified read-only against
/Users/ol/agents/outreach/outreach.db and
/Users/ol/agents/job-search/job_search.db via `sqlite3 -readonly ... .schema`,
and against the real get_hired_prep/index.md + stories/story_bank.md). The
real repos are never opened by this test file.
"""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from agents import config as cfg

TODAY = date(2026, 7, 2)


# --------------------------------------------------------------------------
# fixture builders
# --------------------------------------------------------------------------

_ORG_COLUMNS = [
    "notion_page_id", "organisasjon", "gruppe", "kommune", "nettside",
    "kontaktperson", "kontakt_epost", "kontakt_kilde", "kontakt_rolle",
    "kontakt_hiring", "ekstra_kontakter", "tar_apen_soknad", "varm_hook",
    "fit_score", "reachability", "status", "cv_variant", "sendt_dato",
    "oppfolging_dato", "oppfolging2_dato", "reengasjement_dato", "notater",
    "opprettet",
]

_JOBS_COLUMNS = [
    "Stilling", "Arbeidsgiver", "Sted", "Status", "Soknadsfrist",
    "Stilling URL", "Contact name", "Contact email", "Next action",
    "Notater", "Applied date", "Follow-up date", "Outcome", "page_id",
    "Interview date",
]


def _write_outreach_db(dir_: Path, rows: list[dict]) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    db_path = dir_ / "outreach.db"
    conn = sqlite3.connect(db_path)
    cols_sql = ", ".join(f'"{c}" TEXT' for c in _ORG_COLUMNS)
    conn.execute(f"CREATE TABLE organisasjoner (id INTEGER PRIMARY KEY, {cols_sql})")
    for row in rows:
        cols = list(row.keys())
        placeholders = ",".join("?" * len(cols))
        col_sql = ",".join(f'"{c}"' for c in cols)
        conn.execute(
            f"INSERT INTO organisasjoner ({col_sql}) VALUES ({placeholders})",
            [row[c] for c in cols],
        )
    conn.commit()
    conn.close()
    return db_path


def _write_job_search_db(dir_: Path, rows: list[dict]) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    db_path = dir_ / "job_search.db"
    conn = sqlite3.connect(db_path)
    cols_sql = ", ".join(f'"{c}" TEXT' for c in _JOBS_COLUMNS)
    conn.execute(f"CREATE TABLE jobs ({cols_sql})")
    for row in rows:
        cols = list(row.keys())
        placeholders = ",".join("?" * len(cols))
        col_sql = ",".join(f'"{c}"' for c in cols)
        conn.execute(f"INSERT INTO jobs ({col_sql}) VALUES ({placeholders})", [row[c] for c in cols])
    conn.commit()
    conn.close()
    return db_path


def _write_index_md(prep_root: Path) -> None:
    prep_root.mkdir(parents=True, exist_ok=True)
    (prep_root / "index.md").write_text(
        "# Interview status board\n\n"
        "| Company | Role | Tier | Stage | Interview date | Next step | Folder |\n"
        "|---------|------|------|-------|----------------|-----------|--------|\n"
        "| Test Co | Some Role | T0 | Prepped | 2026-07-10 10:00 | Prep | companies/test-co/ |\n"
        "| Old Co | Old Role | T1 | Finished — no offer | 2026-06-01 09:00 | none | companies/old-co/ |\n"
        "\n"
        "<!-- Example row:\n"
        "| Helseplattformen AS | Radgiver e-helse | T1 | Interview-1 | 2026-06-18 | Drill | companies/helseplattformen/ |\n"
        "-->\n",
        encoding="utf-8",
    )


def _write_prep_company(prep_root: Path, slug: str) -> None:
    company_dir = prep_root / "companies" / slug
    company_dir.mkdir(parents=True, exist_ok=True)
    (company_dir / "company_dossier.md").write_text("Dossier for " + slug, encoding="utf-8")
    (company_dir / "positioning.md").write_text("Positioning for " + slug, encoding="utf-8")
    (company_dir / "interview_plan.md").write_text("Plan for " + slug, encoding="utf-8")
    (company_dir / "log.md").write_text("Tier: T0 — kickoff note\nSecond line\n", encoding="utf-8")


def _write_story_bank(prep_root: Path) -> None:
    stories_dir = prep_root / "stories"
    stories_dir.mkdir(parents=True, exist_ok=True)
    (stories_dir / "story_bank.md").write_text(
        "# Story bank\n\n"
        "### 1. Confirmed story A\n"
        "- **S:** something\n"
        "> Archetypes: tag-a · Confirmed: 2026-06-01 · Last-used: —\n\n"
        "### 2. Unconfirmed story B\n"
        "- **S/T/A/R:** **CONFIRM** — not yet confirmed\n\n",
        encoding="utf-8",
    )


def _default_outreach_rows() -> list[dict]:
    return [
        {  # Sendt, touch-1 due 7 days overdue (within 14-day grace) -> surfaces
            "organisasjon": "Firma A", "gruppe": "G1", "kommune": "Oslo",
            "kontaktperson": "Kari", "kontakt_epost": "kari@firma-a.no",
            "status": "Sendt", "sendt_dato": "2026-06-01",
            "oppfolging_dato": "2026-06-25", "oppfolging2_dato": "",
            "reengasjement_dato": "", "varm_hook": "warm note",
            "notater": "short note about firma a",
        },
        {  # Møte (warm) with a date that WOULD be due -> must never surface
            "organisasjon": "Firma B", "gruppe": "G1", "kommune": "Oslo",
            "kontaktperson": "Ola", "kontakt_epost": "ola@firma-b.no",
            "status": "Møte", "oppfolging_dato": "2026-06-25",
        },
        {  # Sendt but 40 days overdue -> outside grace window, must not surface
            "organisasjon": "Firma C", "gruppe": "G1", "kommune": "Oslo",
            "status": "Sendt", "oppfolging_dato": "2026-05-23",
        },
        {  # Sendt, touch-2 due tomorrow (within 1-day lookahead) -> surfaces
            "organisasjon": "Firma D", "gruppe": "G2", "kommune": "Bergen",
            "kontaktperson": "Per", "kontakt_epost": "per@firma-d.no",
            "status": "Sendt", "oppfolging2_dato": "2026-07-03",
            "notater": "note d",
        },
        {  # Død -> excluded from outreach_due (not Sendt) and from contact_emails
            "organisasjon": "Firma E", "status": "Død",
            "kontakt_epost": "e@firma-e.no", "oppfolging_dato": "2026-06-25",
        },
        {  # Avslag -> excluded from contact_emails
            "organisasjon": "Firma F", "status": "Avslag",
            "kontakt_epost": "f@firma-f.no",
        },
        {  # Blokkert -> excluded from contact_emails
            "organisasjon": "Firma G", "status": "Blokkert",
            "kontakt_epost": "g@firma-g.no",
        },
        {  # Møte -> INCLUDED in contact_emails (warm, only cadence-exempt)
            "organisasjon": "Firma H", "status": "Møte",
            "kontakt_epost": "H@Example.com",
        },
    ]


def _default_jobs_rows() -> list[dict]:
    return [
        {"Stilling": "Engineer I", "Arbeidsgiver": "Acme AS", "Status": "To apply",
         "Soknadsfrist": "2026-07-05", "Next action": "send CV"},
        {"Stilling": "Engineer II", "Arbeidsgiver": "Beta AS", "Status": "To apply",
         "Soknadsfrist": "2026-08-01", "Next action": "later"},
        {"Stilling": "Engineer III", "Arbeidsgiver": "Acme Corp", "Status": "Interview",
         "Follow-up date": "2026-07-08", "Contact email": "Recruiter@Acme.com"},
        {"Stilling": "Engineer IV", "Arbeidsgiver": "Gamma AS", "Status": "Applied",
         "Contact email": "applied@x.no"},
        {"Stilling": "Engineer V", "Arbeidsgiver": "Delta AS", "Status": "Rejected",
         "Contact email": "rejected@x.no"},
        {"Stilling": "Engineer VI", "Arbeidsgiver": "Epsilon AS", "Status": "To apply",
         "Soknadsfrist": ""},
    ]


def _patch_cfg(monkeypatch, roots: dict[str, Path], **overrides):
    orig_get = cfg.get
    data: dict = {f"jobhunt.roots.{k}": str(v) for k, v in roots.items()}
    data.update(overrides)

    def fake_get(key, default=None):
        if key in data:
            return data[key]
        return orig_get(key, default)

    monkeypatch.setattr(cfg, "get", fake_get)


@pytest.fixture
def full_fixture(tmp_path, monkeypatch):
    outreach_dir = tmp_path / "outreach"
    job_search_dir = tmp_path / "job-search"
    prep_dir = tmp_path / "get_hired_prep"

    outreach_db = _write_outreach_db(outreach_dir, _default_outreach_rows())
    job_search_db = _write_job_search_db(job_search_dir, _default_jobs_rows())
    _write_index_md(prep_dir)
    _write_prep_company(prep_dir, "test-co")
    _write_story_bank(prep_dir)

    _patch_cfg(monkeypatch, {
        "outreach": outreach_dir, "job_search": job_search_dir, "prep": prep_dir,
    })

    return {
        "outreach_db": outreach_db,
        "job_search_db": job_search_db,
        "prep_dir": prep_dir,
    }


# --------------------------------------------------------------------------
# outreach_due
# --------------------------------------------------------------------------

def test_outreach_due_detects_touch1_with_correct_days_overdue(full_fixture):
    from tools.jobhunt import readers

    due = readers.outreach_due(TODAY)
    firma_a = next(d for d in due if d["org"] == "Firma A")
    assert firma_a["touch"] == "1"
    assert firma_a["due"] == "2026-06-25"
    assert firma_a["days_overdue"] == 7
    assert firma_a["kontakt"] == "Kari"
    assert firma_a["epost"] == "kari@firma-a.no"
    assert firma_a["varm_hook"] == "warm note"
    assert firma_a["notater_tail"] == "short note about firma a"


def test_outreach_due_excludes_mote_row_hard_constraint(full_fixture):
    """Warm-contact exemption: a status='Møte' row must NEVER surface in
    outreach_due, even when its date would otherwise be due."""
    from tools.jobhunt import readers

    due = readers.outreach_due(TODAY)
    orgs = [d["org"] for d in due]
    assert "Firma B" not in orgs


def test_outreach_due_excludes_dod_avslag_blokkert(full_fixture):
    from tools.jobhunt import readers

    due = readers.outreach_due(TODAY)
    orgs = [d["org"] for d in due]
    assert "Firma E" not in orgs  # Død
    assert "Firma F" not in orgs  # Avslag
    assert "Firma G" not in orgs  # Blokkert


def test_outreach_due_excludes_touch_outside_grace_window(full_fixture):
    from tools.jobhunt import readers

    due = readers.outreach_due(TODAY)
    orgs = [d["org"] for d in due]
    assert "Firma C" not in orgs  # 40 days overdue > 14-day grace


def test_outreach_due_includes_touch_within_lookahead(full_fixture):
    from tools.jobhunt import readers

    due = readers.outreach_due(TODAY)
    firma_d = next(d for d in due if d["org"] == "Firma D")
    assert firma_d["touch"] == "2"
    assert firma_d["days_overdue"] == -1  # due tomorrow


def test_outreach_due_sorted_by_due_date(full_fixture):
    from tools.jobhunt import readers

    due = readers.outreach_due(TODAY)
    dues = [d["due"] for d in due]
    assert dues == sorted(dues)


def test_outreach_due_dedups_same_org_across_rows_keeps_earliest(tmp_path, monkeypatch):
    """Real data has NTNU IHA tracked under more than one gruppe row.
    outreach_due must dedup across DB ROWS sharing organisasjon, keeping
    only the earliest-due entry — never one entry per row."""
    from tools.jobhunt import readers

    outreach_dir = tmp_path / "outreach"
    _write_outreach_db(outreach_dir, [
        {"organisasjon": "NTNU IHA", "gruppe": "G1", "status": "Sendt",
         "oppfolging_dato": "2026-06-20"},
        {"organisasjon": "NTNU IHA", "gruppe": "G2", "status": "Sendt",
         "oppfolging_dato": "2026-06-25"},
    ])

    _patch_cfg(monkeypatch, {
        "outreach": outreach_dir,
        "job_search": tmp_path / "no-job-search",
        "prep": tmp_path / "no-prep",
    })

    due = readers.outreach_due(TODAY)
    ntnu_entries = [d for d in due if d["org"] == "NTNU IHA"]
    assert len(ntnu_entries) == 1
    assert ntnu_entries[0]["due"] == "2026-06-20"


# --------------------------------------------------------------------------
# pipeline_summary
# --------------------------------------------------------------------------

def test_pipeline_summary_counts_by_status(full_fixture):
    from tools.jobhunt import readers

    summary = readers.pipeline_summary()
    assert summary["outreach"]["Sendt"] == 3   # Firma A, C, D
    assert summary["outreach"]["Møte"] == 2    # Firma B, H
    assert summary["applications"]["To apply"] == 3
    assert summary["applications"]["Interview"] == 1
    assert summary["applications"]["Applied"] == 1


# --------------------------------------------------------------------------
# application_deadlines
# --------------------------------------------------------------------------

def test_application_deadlines_window(full_fixture):
    from tools.jobhunt import readers

    deadlines = readers.application_deadlines(TODAY)
    names = [d["org"] for d in deadlines]
    assert "Acme AS" in names       # 2026-07-05 inside 7-day window
    assert "Beta AS" not in names   # 2026-08-01 outside window
    assert "Epsilon AS" not in names  # blank Soknadsfrist


def test_application_deadlines_sorted_ascending(full_fixture):
    from tools.jobhunt import readers

    deadlines = readers.application_deadlines(TODAY)
    frister = [d["frist"] for d in deadlines]
    assert frister == sorted(frister)


# --------------------------------------------------------------------------
# interviews_upcoming
# --------------------------------------------------------------------------

def test_interviews_upcoming_unions_jobs_and_prep(full_fixture):
    from tools.jobhunt import readers

    interviews = readers.interviews_upcoming(TODAY)
    by_org = {i["org"]: i for i in interviews}

    assert "Acme Corp" in by_org
    assert by_org["Acme Corp"]["source"] == "jobs"
    assert by_org["Acme Corp"]["slug"] == "acme-corp"
    # jobs-sourced rows never carry a date — "Follow-up date" is banned.
    assert by_org["Acme Corp"]["date"] is None

    assert "Test Co" in by_org
    prep_entry = by_org["Test Co"]
    assert prep_entry["source"] == "prep"
    assert prep_entry["slug"] == "test-co"
    assert prep_entry["date"] == "2026-07-10"
    assert prep_entry["prep_state"] == "Prepped"


def test_interviews_upcoming_excludes_past_prep_row(full_fixture):
    from tools.jobhunt import readers

    interviews = readers.interviews_upcoming(TODAY)
    orgs = [i["org"] for i in interviews]
    assert "Old Co" not in orgs  # 2026-06-01 is in the past


def test_interviews_upcoming_ignores_html_commented_example_row(full_fixture):
    from tools.jobhunt import readers

    interviews = readers.interviews_upcoming(TODAY)
    orgs = [i["org"] for i in interviews]
    assert "Helseplattformen AS" not in orgs


def test_interviews_upcoming_jobs_source_never_uses_followup_date(tmp_path, monkeypatch):
    """CRITICAL: jobs-sourced interview rows must never surface the
    "Follow-up date" column as their date — it's a banned-legacy field,
    provably wrong (real DNB row: Follow-up date 2026-07-10 for an
    interview that actually happened 2026-06-29 per index.md)."""
    from tools.jobhunt import readers

    outreach_dir = tmp_path / "outreach"
    job_search_dir = tmp_path / "job-search"
    prep_dir = tmp_path / "get_hired_prep"

    _write_outreach_db(outreach_dir, [])
    _write_job_search_db(job_search_dir, [
        {"Stilling": "Some Role", "Arbeidsgiver": "Solo Corp", "Status": "Interview",
         "Follow-up date": "2026-07-10"},
    ])
    prep_dir.mkdir(parents=True, exist_ok=True)

    _patch_cfg(monkeypatch, {
        "outreach": outreach_dir, "job_search": job_search_dir, "prep": prep_dir,
    })

    interviews = readers.interviews_upcoming(TODAY)
    solo = next(i for i in interviews if i["org"] == "Solo Corp")
    assert solo["date"] is None
    assert solo["source"] == "jobs"


def test_interviews_upcoming_suppresses_jobs_row_for_past_held_interview(tmp_path, monkeypatch):
    """Real DNB regression: index.md has a PAST-dated row (interview already
    held), jobs.db still carries Status='Interview' awaiting outcome. The
    jobs row must be suppressed entirely — no "date TBD" duplicate for an
    interview that already happened."""
    from tools.jobhunt import readers

    outreach_dir = tmp_path / "outreach"
    job_search_dir = tmp_path / "job-search"
    prep_dir = tmp_path / "get_hired_prep"

    _write_outreach_db(outreach_dir, [])
    _write_job_search_db(job_search_dir, [
        {"Stilling": "Platform Engineer, Radical AI (RAI)", "Arbeidsgiver": "DNB Bank ASA (Radical AI)",
         "Status": "Interview", "Follow-up date": "2026-07-10"},
    ])
    prep_dir.mkdir(parents=True, exist_ok=True)
    (prep_dir / "index.md").write_text(
        "| Company | Role | Tier | Stage | Interview date | Next step | Folder |\n"
        "|---------|------|------|-------|----------------|-----------|--------|\n"
        "| DNB Bank ASA | Platform Engineer, Radical AI (RAI) | T0 | Prepped |"
        " 2026-06-29 11:15 | Mock drill | companies/dnb-radical-ai/ |\n",
        encoding="utf-8",
    )

    _patch_cfg(monkeypatch, {
        "outreach": outreach_dir, "job_search": job_search_dir, "prep": prep_dir,
    })

    interviews = readers.interviews_upcoming(TODAY)
    dnb_entries = [i for i in interviews if "dnb" in i["org"].lower()]
    assert dnb_entries == []


def test_interviews_upcoming_future_index_row_still_emits_dated_entry(tmp_path, monkeypatch):
    """Sanity companion to the past-held-interview suppression test: a
    FUTURE-dated index row must still emit a dated prep entry as before —
    the fix must not regress the existing future-date path."""
    from tools.jobhunt import readers

    outreach_dir = tmp_path / "outreach"
    job_search_dir = tmp_path / "job-search"
    prep_dir = tmp_path / "get_hired_prep"

    _write_outreach_db(outreach_dir, [])
    _write_job_search_db(job_search_dir, [
        {"Stilling": "Platform Engineer, Radical AI", "Arbeidsgiver": "DNB Bank ASA (Radical AI)",
         "Status": "Interview", "Follow-up date": "2026-07-10"},
    ])
    prep_dir.mkdir(parents=True, exist_ok=True)
    (prep_dir / "index.md").write_text(
        "| Company | Role | Tier | Stage | Interview date | Next step | Folder |\n"
        "|---------|------|------|-------|----------------|-----------|--------|\n"
        "| DNB Bank ASA | Platform Engineer, Radical AI (RAI) | T0 | Prepped |"
        " 2026-07-20 11:15 | Mock drill | companies/dnb-radical-ai/ |\n",
        encoding="utf-8",
    )

    _patch_cfg(monkeypatch, {
        "outreach": outreach_dir, "job_search": job_search_dir, "prep": prep_dir,
    })

    interviews = readers.interviews_upcoming(TODAY)
    dnb_entries = [i for i in interviews if "dnb" in i["org"].lower()]
    assert len(dnb_entries) == 1
    merged = dnb_entries[0]
    assert merged["source"] == "prep"
    assert merged["date"] == "2026-07-20"
    assert merged["slug"] == "dnb-radical-ai"


def test_interviews_upcoming_jobs_row_without_index_match_still_emits_date_none(tmp_path, monkeypatch):
    """A jobs Interview row with NO matching index row at all (past or
    future) must still emit date=None exactly as before — unmatched
    behavior is unchanged by the past-row suppression fix."""
    from tools.jobhunt import readers

    outreach_dir = tmp_path / "outreach"
    job_search_dir = tmp_path / "job-search"
    prep_dir = tmp_path / "get_hired_prep"

    _write_outreach_db(outreach_dir, [])
    _write_job_search_db(job_search_dir, [
        {"Stilling": "Some Role", "Arbeidsgiver": "Unmatched Corp",
         "Status": "Interview"},
    ])
    prep_dir.mkdir(parents=True, exist_ok=True)
    (prep_dir / "index.md").write_text(
        "| Company | Role | Tier | Stage | Interview date | Next step | Folder |\n"
        "|---------|------|------|-------|----------------|-----------|--------|\n"
        "| Totally Different Co | Other Role | T0 | Prepped |"
        " 2026-06-01 09:00 | done | companies/totally-different-co/ |\n",
        encoding="utf-8",
    )

    _patch_cfg(monkeypatch, {
        "outreach": outreach_dir, "job_search": job_search_dir, "prep": prep_dir,
    })

    interviews = readers.interviews_upcoming(TODAY)
    unmatched = next(i for i in interviews if i["org"] == "Unmatched Corp")
    assert unmatched["date"] is None
    assert unmatched["source"] == "jobs"


def test_interviews_upcoming_merges_jobs_and_index_dnb_triple(tmp_path, monkeypatch):
    """Real-world triple that MUST merge into one entry: jobs "DNB Bank ASA
    (Radical AI)" vs index "DNB Bank ASA" vs slug dnb-radical-ai. The merged
    entry must be index-sourced (dated), not the jobs-sourced duplicate."""
    from tools.jobhunt import readers

    outreach_dir = tmp_path / "outreach"
    job_search_dir = tmp_path / "job-search"
    prep_dir = tmp_path / "get_hired_prep"

    _write_outreach_db(outreach_dir, [])
    _write_job_search_db(job_search_dir, [
        {"Stilling": "Platform Engineer, Radical AI", "Arbeidsgiver": "DNB Bank ASA (Radical AI)",
         "Status": "Interview", "Follow-up date": "2026-07-10"},
    ])
    prep_dir.mkdir(parents=True, exist_ok=True)
    (prep_dir / "index.md").write_text(
        "| Company | Role | Tier | Stage | Interview date | Next step | Folder |\n"
        "|---------|------|------|-------|----------------|-----------|--------|\n"
        "| DNB Bank ASA | Platform Engineer, Radical AI (RAI) | T0 | Prepped |"
        " 2026-07-05 11:15 | Mock drill | companies/dnb-radical-ai/ |\n",
        encoding="utf-8",
    )

    _patch_cfg(monkeypatch, {
        "outreach": outreach_dir, "job_search": job_search_dir, "prep": prep_dir,
    })

    interviews = readers.interviews_upcoming(TODAY)
    dnb_entries = [i for i in interviews if "dnb" in i["org"].lower()]
    assert len(dnb_entries) == 1
    merged = dnb_entries[0]
    assert merged["source"] == "prep"
    assert merged["org"] == "DNB Bank ASA"
    assert merged["date"] == "2026-07-05"
    assert merged["slug"] == "dnb-radical-ai"


def test_interviews_upcoming_jobs_interview_date_future_emits_dated_entry_no_index_needed(
    tmp_path, monkeypatch,
):
    """Task 3 (2026-07-12 backlog wave): jobs.db's OWN "Interview date"
    column, once mail_triage starts writing it, is now the primary dated
    source for a jobs-sourced entry — no get_hired_prep/index.md row is
    needed at all."""
    from tools.jobhunt import readers

    outreach_dir = tmp_path / "outreach"
    job_search_dir = tmp_path / "job-search"
    prep_dir = tmp_path / "get_hired_prep"

    _write_outreach_db(outreach_dir, [])
    _write_job_search_db(job_search_dir, [
        {"Stilling": "Platform Engineer, Radical AI", "Arbeidsgiver": "DNB Bank ASA (Radical AI)",
         "Status": "Interview", "Interview date": "2026-08-05"},
    ])
    prep_dir.mkdir(parents=True, exist_ok=True)   # no index.md at all

    _patch_cfg(monkeypatch, {
        "outreach": outreach_dir, "job_search": job_search_dir, "prep": prep_dir,
    })

    interviews = readers.interviews_upcoming(TODAY)
    dnb = next(i for i in interviews if "dnb" in i["org"].lower())
    assert dnb["date"] == "2026-08-05"
    assert dnb["source"] == "jobs"
    # No Folder to borrow a slug from (no index.md row) -- slugified straight
    # from the Arbeidsgiver name, same as any other undated jobs-only entry.
    assert dnb["slug"] == "dnb-bank-asa-radical-ai"


def test_interviews_upcoming_jobs_interview_date_past_is_suppressed(tmp_path, monkeypatch):
    """A past jobs.Interview date means the interview already happened — the
    row must be suppressed entirely, never surfaced as a stale entry."""
    from tools.jobhunt import readers

    outreach_dir = tmp_path / "outreach"
    job_search_dir = tmp_path / "job-search"
    prep_dir = tmp_path / "get_hired_prep"

    _write_outreach_db(outreach_dir, [])
    _write_job_search_db(job_search_dir, [
        {"Stilling": "Some Role", "Arbeidsgiver": "Past Interview Corp",
         "Status": "Interview", "Interview date": "2026-06-29"},
    ])
    prep_dir.mkdir(parents=True, exist_ok=True)

    _patch_cfg(monkeypatch, {
        "outreach": outreach_dir, "job_search": job_search_dir, "prep": prep_dir,
    })

    interviews = readers.interviews_upcoming(TODAY)
    assert not any("past interview corp" in i["org"].lower() for i in interviews)


def test_interviews_upcoming_jobs_interview_date_empty_falls_back_to_index(
    tmp_path, monkeypatch,
):
    """An empty "Interview date" column must behave EXACTLY as before the
    column existed — index.md stays the fallback dated source, and an
    unmatched row still emits date=None."""
    from tools.jobhunt import readers

    outreach_dir = tmp_path / "outreach"
    job_search_dir = tmp_path / "job-search"
    prep_dir = tmp_path / "get_hired_prep"

    _write_outreach_db(outreach_dir, [])
    _write_job_search_db(job_search_dir, [
        {"Stilling": "Platform Engineer, Radical AI", "Arbeidsgiver": "DNB Bank ASA (Radical AI)",
         "Status": "Interview", "Interview date": ""},
        {"Stilling": "Some Role", "Arbeidsgiver": "Unmatched Corp",
         "Status": "Interview", "Interview date": ""},
    ])
    prep_dir.mkdir(parents=True, exist_ok=True)
    (prep_dir / "index.md").write_text(
        "| Company | Role | Tier | Stage | Interview date | Next step | Folder |\n"
        "|---------|------|------|-------|----------------|-----------|--------|\n"
        "| DNB Bank ASA | Platform Engineer, Radical AI (RAI) | T0 | Prepped |"
        " 2026-07-20 11:15 | Mock drill | companies/dnb-radical-ai/ |\n",
        encoding="utf-8",
    )

    _patch_cfg(monkeypatch, {
        "outreach": outreach_dir, "job_search": job_search_dir, "prep": prep_dir,
    })

    interviews = readers.interviews_upcoming(TODAY)
    dnb = next(i for i in interviews if "dnb" in i["org"].lower())
    assert dnb["source"] == "prep"        # index.md fallback still wins here
    assert dnb["date"] == "2026-07-20"
    unmatched = next(i for i in interviews if i["org"] == "Unmatched Corp")
    assert unmatched["date"] is None      # unmatched row: unchanged fallback
    assert unmatched["source"] == "jobs"


def test_interviews_upcoming_jobs_interview_date_wins_drops_duplicate_index_entry(
    tmp_path, monkeypatch,
):
    """Review fix (2026-07-12): a jobs row with its OWN "Interview date" set
    that ALSO fuzzy-matches a future get_hired_prep/index.md row for the same
    org used to emit BOTH — the jobs entry (source="jobs") AND the index
    entry (source="prep") — double-booking the same interview in the hikari
    brief. jobs wins: exactly one entry, the jobs one, and the index/prep
    duplicate for that org is dropped."""
    from tools.jobhunt import readers

    outreach_dir = tmp_path / "outreach"
    job_search_dir = tmp_path / "job-search"
    prep_dir = tmp_path / "get_hired_prep"

    _write_outreach_db(outreach_dir, [])
    _write_job_search_db(job_search_dir, [
        {"Stilling": "Platform Engineer, Radical AI", "Arbeidsgiver": "DNB Bank ASA (Radical AI)",
         "Status": "Interview", "Interview date": "2026-07-20"},
    ])
    prep_dir.mkdir(parents=True, exist_ok=True)
    (prep_dir / "index.md").write_text(
        "| Company | Role | Tier | Stage | Interview date | Next step | Folder |\n"
        "|---------|------|------|-------|----------------|-----------|--------|\n"
        "| DNB Bank ASA | Platform Engineer, Radical AI (RAI) | T0 | Prepped |"
        " 2026-07-20 11:15 | Mock drill | companies/dnb-radical-ai/ |\n",
        encoding="utf-8",
    )

    _patch_cfg(monkeypatch, {
        "outreach": outreach_dir, "job_search": job_search_dir, "prep": prep_dir,
    })

    interviews = readers.interviews_upcoming(TODAY)
    dnb_entries = [i for i in interviews if "dnb" in i["org"].lower()]
    assert len(dnb_entries) == 1
    assert dnb_entries[0]["source"] == "jobs"
    assert dnb_entries[0]["date"] == "2026-07-20"


def test_interviews_upcoming_jobs_interview_date_no_index_match_single_entry(
    tmp_path, monkeypatch,
):
    """Companion sanity check: a jobs Interview date with no index.md row at
    all for that org must still emit exactly one entry (no suppression logic
    accidentally drops it when there is nothing to suppress against)."""
    from tools.jobhunt import readers

    outreach_dir = tmp_path / "outreach"
    job_search_dir = tmp_path / "job-search"
    prep_dir = tmp_path / "get_hired_prep"

    _write_outreach_db(outreach_dir, [])
    _write_job_search_db(job_search_dir, [
        {"Stilling": "Some Role", "Arbeidsgiver": "Solo Interview Corp",
         "Status": "Interview", "Interview date": "2026-08-05"},
    ])
    prep_dir.mkdir(parents=True, exist_ok=True)

    _patch_cfg(monkeypatch, {
        "outreach": outreach_dir, "job_search": job_search_dir, "prep": prep_dir,
    })

    interviews = readers.interviews_upcoming(TODAY)
    matches = [i for i in interviews if i["org"] == "Solo Interview Corp"]
    assert len(matches) == 1
    assert matches[0]["source"] == "jobs"
    assert matches[0]["date"] == "2026-08-05"


def test_interviews_upcoming_empty_jobs_date_with_index_row_single_prep_entry(
    tmp_path, monkeypatch,
):
    """Companion sanity check: an EMPTY jobs Interview date with a matching
    index.md row must still emit exactly one entry (the prep one, unchanged
    fallback behaviour) — the jobs-wins fix must not touch this path."""
    from tools.jobhunt import readers

    outreach_dir = tmp_path / "outreach"
    job_search_dir = tmp_path / "job-search"
    prep_dir = tmp_path / "get_hired_prep"

    _write_outreach_db(outreach_dir, [])
    _write_job_search_db(job_search_dir, [
        {"Stilling": "Platform Engineer, Radical AI", "Arbeidsgiver": "DNB Bank ASA (Radical AI)",
         "Status": "Interview", "Interview date": ""},
    ])
    prep_dir.mkdir(parents=True, exist_ok=True)
    (prep_dir / "index.md").write_text(
        "| Company | Role | Tier | Stage | Interview date | Next step | Folder |\n"
        "|---------|------|------|-------|----------------|-----------|--------|\n"
        "| DNB Bank ASA | Platform Engineer, Radical AI (RAI) | T0 | Prepped |"
        " 2026-07-20 11:15 | Mock drill | companies/dnb-radical-ai/ |\n",
        encoding="utf-8",
    )

    _patch_cfg(monkeypatch, {
        "outreach": outreach_dir, "job_search": job_search_dir, "prep": prep_dir,
    })

    interviews = readers.interviews_upcoming(TODAY)
    dnb_entries = [i for i in interviews if "dnb" in i["org"].lower()]
    assert len(dnb_entries) == 1
    assert dnb_entries[0]["source"] == "prep"
    assert dnb_entries[0]["date"] == "2026-07-20"


# --------------------------------------------------------------------------
# org_context
# --------------------------------------------------------------------------

def test_org_context_unique_match_returns_full_row(full_fixture):
    from tools.jobhunt import readers

    ctx = readers.org_context("firma a")
    assert ctx is not None
    assert ctx["organisasjon"] == "Firma A"
    assert ctx["kontaktperson"] == "Kari"


def test_org_context_ambiguous_match_returns_ambiguous_shape(full_fixture):
    from tools.jobhunt import readers

    ctx = readers.org_context("firma")
    assert ctx is not None
    assert set(ctx.keys()) == {"ambiguous"}
    assert "Firma A" in ctx["ambiguous"]
    assert "Firma B" in ctx["ambiguous"]


def test_org_context_no_match_returns_none(full_fixture):
    from tools.jobhunt import readers

    assert readers.org_context("nonexistent-org-xyz") is None


# --------------------------------------------------------------------------
# prep_files
# --------------------------------------------------------------------------

def test_prep_files_reads_company_files_and_tier(full_fixture):
    from tools.jobhunt import readers

    files = readers.prep_files("test-co")
    assert files["company_dossier"] == "Dossier for test-co"
    assert files["positioning"] == "Positioning for test-co"
    assert files["interview_plan"] == "Plan for test-co"
    assert files["tier"] == "Tier: T0 — kickoff note"


def test_prep_files_only_returns_confirmed_stories(full_fixture):
    from tools.jobhunt import readers

    files = readers.prep_files("test-co")
    stories = files["confirmed_stories"]
    joined = "\n".join(stories)
    assert "Confirmed story A" in joined
    assert "Unconfirmed story B" not in joined


def test_prep_files_unknown_slug_returns_empty_dict(full_fixture):
    from tools.jobhunt import readers

    assert readers.prep_files("no-such-company") == {}


# --------------------------------------------------------------------------
# contact_emails
# --------------------------------------------------------------------------

def test_contact_emails_unions_and_lowercases(full_fixture):
    from tools.jobhunt import readers

    emails = readers.contact_emails()
    assert "kari@firma-a.no" in emails
    assert "h@example.com" in emails          # Møte row, lowercased
    assert "recruiter@acme.com" in emails     # jobs Interview row, lowercased
    assert "applied@x.no" in emails           # jobs Applied row


def test_contact_emails_excludes_dod_avslag_blokkert(full_fixture):
    from tools.jobhunt import readers

    emails = readers.contact_emails()
    assert "e@firma-e.no" not in emails   # Død
    assert "f@firma-f.no" not in emails   # Avslag
    assert "g@firma-g.no" not in emails   # Blokkert


def test_contact_emails_excludes_jobs_rejected_status(full_fixture):
    from tools.jobhunt import readers

    emails = readers.contact_emails()
    assert "rejected@x.no" not in emails


# --------------------------------------------------------------------------
# missing / corrupt DB -> every reader returns empty, never raises
# --------------------------------------------------------------------------

def test_readers_return_empty_on_missing_root_dir(tmp_path, monkeypatch):
    from tools.jobhunt import readers

    _patch_cfg(monkeypatch, {
        "outreach": tmp_path / "does-not-exist-outreach",
        "job_search": tmp_path / "does-not-exist-job-search",
        "prep": tmp_path / "does-not-exist-prep",
    })

    assert readers.outreach_due(TODAY) == []
    assert readers.pipeline_summary() == {"outreach": {}, "applications": {}}
    assert readers.application_deadlines(TODAY) == []
    assert readers.interviews_upcoming(TODAY) == []
    assert readers.org_context("anything") is None
    assert readers.prep_files("anything") == {}
    assert readers.contact_emails() == set()


def test_readers_return_empty_on_existing_root_missing_db_file(tmp_path, monkeypatch):
    from tools.jobhunt import readers

    outreach_dir = tmp_path / "outreach-empty"
    job_search_dir = tmp_path / "job-search-empty"
    prep_dir = tmp_path / "prep-empty"
    outreach_dir.mkdir()
    job_search_dir.mkdir()
    prep_dir.mkdir()

    _patch_cfg(monkeypatch, {
        "outreach": outreach_dir, "job_search": job_search_dir, "prep": prep_dir,
    })

    assert readers.outreach_due(TODAY) == []
    assert readers.pipeline_summary() == {"outreach": {}, "applications": {}}
    assert readers.application_deadlines(TODAY) == []
    assert readers.contact_emails() == set()


def test_readers_return_empty_on_corrupt_db(tmp_path, monkeypatch):
    from tools.jobhunt import readers

    outreach_dir = tmp_path / "outreach-corrupt"
    outreach_dir.mkdir()
    (outreach_dir / "outreach.db").write_bytes(b"not a real sqlite database")

    _patch_cfg(monkeypatch, {
        "outreach": outreach_dir,
        "job_search": tmp_path / "no-job-search",
        "prep": tmp_path / "no-prep",
    })

    assert readers.outreach_due(TODAY) == []
    assert readers.contact_emails() == set()
    summary = readers.pipeline_summary()
    assert summary["outreach"] == {}


# --------------------------------------------------------------------------
# READ-ONLY PROOF — belt-and-braces on the mode=ro contract
# --------------------------------------------------------------------------

def test_read_only_proof_fixture_dbs_untouched_after_all_readers(full_fixture):
    from tools.jobhunt import readers

    outreach_db = full_fixture["outreach_db"]
    job_search_db = full_fixture["job_search_db"]

    before = {
        outreach_db: (outreach_db.stat().st_mtime_ns, outreach_db.read_bytes()),
        job_search_db: (job_search_db.stat().st_mtime_ns, job_search_db.read_bytes()),
    }

    readers.outreach_due(TODAY)
    readers.pipeline_summary()
    readers.application_deadlines(TODAY)
    readers.interviews_upcoming(TODAY)
    readers.org_context("firma a")
    readers.prep_files("test-co")
    readers.contact_emails()

    for db_path, (mtime_ns, content) in before.items():
        after_stat = db_path.stat()
        assert after_stat.st_mtime_ns == mtime_ns, f"{db_path} mtime changed"
        assert db_path.read_bytes() == content, f"{db_path} bytes changed"

    # No stray journal/wal/shm side files left behind either.
    for db_path in (outreach_db, job_search_db):
        siblings = list(db_path.parent.iterdir())
        assert not any(p.name.startswith(db_path.name + "-") for p in siblings)
