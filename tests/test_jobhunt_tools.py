"""Tests for the jobhunt MCP tool handlers (Task 2) — jobhunt_radar,
jobhunt_org, jobhunt_prep — and the static tools.yaml/catalog registration.

Fixtures build throwaway sqlite DBs + markdown trees under tmp_path,
same shapes as tests/test_jobhunt_readers.py (Task 1). This file tests
the HANDLER layer: rendering, capping, and — the whole point of Task 2 —
selective untrusted-content wrapping (free-text fields wrapped, dates/
counts/touch-labels left plain).
"""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest
import yaml

from agents import config as cfg

TODAY = date(2026, 7, 2)
UNTRUSTED_MARK = "<<<HIKARI_UNTRUSTED_"

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
def radar_fixture(tmp_path, monkeypatch):
    """One org due today, one deadline in-window, one prep-sourced interview
    upcoming, one job-sourced interview. Mirrors the shapes exercised by
    tests/test_jobhunt_readers.py but trimmed to what the handler tests need."""
    outreach_dir = tmp_path / "outreach"
    job_search_dir = tmp_path / "job-search"
    prep_dir = tmp_path / "get_hired_prep"

    _write_outreach_db(outreach_dir, [
        {
            "organisasjon": "Firma A", "gruppe": "G1", "kontaktperson": "Kari",
            "kontakt_epost": "kari@firma-a.no", "status": "Sendt",
            "oppfolging_dato": "2026-06-25", "varm_hook": "warm note",
            "notater": "short note about firma a",
        },
    ])
    _write_job_search_db(job_search_dir, [
        {"Stilling": "Engineer I", "Arbeidsgiver": "Acme AS", "Status": "To apply",
         "Soknadsfrist": "2026-07-05", "Next action": "send CV"},
        {"Stilling": "Engineer III", "Arbeidsgiver": "Acme Corp", "Status": "Interview",
         "Follow-up date": "2026-07-08"},
    ])
    prep_dir.mkdir(parents=True, exist_ok=True)
    (prep_dir / "index.md").write_text(
        "| Company | Role | Tier | Stage | Interview date | Next step | Folder |\n"
        "|---------|------|------|-------|----------------|-----------|--------|\n"
        "| Test Co | Some Role | T0 | Prepped | 2026-07-10 10:00 | Prep | companies/test-co/ |\n",
        encoding="utf-8",
    )
    _write_prep_company(prep_dir, "test-co")
    _write_story_bank(prep_dir)

    _patch_cfg(monkeypatch, {
        "outreach": outreach_dir, "job_search": job_search_dir, "prep": prep_dir,
    })

    from tools.jobhunt import handlers
    monkeypatch.setattr(handlers, "_today", lambda: TODAY)
    return handlers


@pytest.fixture
def org_fixture(tmp_path, monkeypatch):
    outreach_dir = tmp_path / "outreach"
    _write_outreach_db(outreach_dir, [
        {
            "organisasjon": "Firma A", "gruppe": "G1", "kontaktperson": "Kari",
            "kontakt_epost": "kari@firma-a.no", "status": "Sendt",
            "varm_hook": "warm note", "notater": "note about firma a",
        },
        {"organisasjon": "Firma B", "gruppe": "G1", "status": "Sendt"},
    ])
    _patch_cfg(monkeypatch, {
        "outreach": outreach_dir,
        "job_search": tmp_path / "no-job-search",
        "prep": tmp_path / "no-prep",
    })
    from tools.jobhunt import handlers
    return handlers


@pytest.fixture
def prep_fixture(tmp_path, monkeypatch):
    prep_dir = tmp_path / "get_hired_prep"
    prep_dir.mkdir(parents=True, exist_ok=True)
    _write_prep_company(prep_dir, "test-co")
    _write_story_bank(prep_dir)
    _patch_cfg(monkeypatch, {
        "outreach": tmp_path / "no-outreach",
        "job_search": tmp_path / "no-job-search",
        "prep": prep_dir,
    })
    from tools.jobhunt import handlers
    return handlers


# --------------------------------------------------------------------------
# jobhunt_radar
# --------------------------------------------------------------------------

async def test_radar_wraps_org_name_in_untrusted_markers(radar_fixture):
    result = await radar_fixture.radar({})
    text = result["content"][0]["text"]
    assert UNTRUSTED_MARK in text


async def test_radar_leaves_dates_and_touch_labels_unwrapped(radar_fixture):
    result = await radar_fixture.radar({})
    text = result["content"][0]["text"]
    assert "touch 1" in text
    assert "2026-06-25" in text
    assert "presentation_hint" in text


async def test_radar_wraps_org_in_data_payload_too(radar_fixture):
    """The `data` dict is JSON-dumped straight into the visible text body
    by tools._response.ok — so free-text fields must be wrapped BEFORE
    they land in `data`, not just in the narrative lines."""
    result = await radar_fixture.radar({})
    due = result["data"]["outreach_due"]
    assert len(due) == 1
    assert UNTRUSTED_MARK in due[0]["org"]
    assert due[0]["due"] == "2026-06-25"          # unwrapped
    assert due[0]["days_overdue"] == 7             # unwrapped


async def test_radar_wraps_deadline_org_and_job_title(radar_fixture):
    result = await radar_fixture.radar({})
    deadlines = result["data"]["application_deadlines"]
    acme = next(d for d in deadlines if UNTRUSTED_MARK in d["org"])
    assert UNTRUSTED_MARK in acme["stilling"]
    assert acme["frist"] == "2026-07-05"           # unwrapped


async def test_radar_wraps_interview_org(radar_fixture):
    result = await radar_fixture.radar({})
    interviews = result["data"]["interviews_upcoming"]
    assert interviews
    for i in interviews:
        assert UNTRUSTED_MARK in i["org"]


async def test_radar_pipeline_summary_present_and_unwrapped(radar_fixture):
    result = await radar_fixture.radar({})
    summary = result["data"]["pipeline_summary"]
    assert summary["outreach"]["Sendt"] == 1
    assert summary["applications"]["To apply"] == 1


async def test_radar_sections_capped_at_five_with_count_note(tmp_path, monkeypatch):
    outreach_dir = tmp_path / "outreach"
    rows = [
        {
            "organisasjon": f"Firma {i}", "gruppe": "G1", "status": "Sendt",
            "oppfolging_dato": "2026-06-25",
        }
        for i in range(8)
    ]
    _write_outreach_db(outreach_dir, rows)
    _patch_cfg(monkeypatch, {
        "outreach": outreach_dir,
        "job_search": tmp_path / "no-job-search",
        "prep": tmp_path / "no-prep",
    })
    from tools.jobhunt import handlers
    monkeypatch.setattr(handlers, "_today", lambda: TODAY)

    result = await handlers.radar({})
    text = result["content"][0]["text"]
    due = result["data"]["outreach_due"]
    assert len(due) == 8                    # full data preserved
    assert "outreach due (8)" in text
    assert "+3 more" in text                 # 8 - 5 shown = 3 more
    # 5 org bullet lines rendered even though all 8 due entries share touch "1"
    assert text.count("touch 1") == 5


# --------------------------------------------------------------------------
# jobhunt_org
# --------------------------------------------------------------------------

async def test_org_ambiguous_returns_candidate_list(org_fixture):
    result = await org_fixture.org({"name": "firma"})
    assert "ambiguous" in result["data"]
    candidates = result["data"]["ambiguous"]
    assert len(candidates) == 2
    joined = "\n".join(candidates)
    assert "Firma A" in joined
    assert "Firma B" in joined
    assert UNTRUSTED_MARK in joined


async def test_org_not_found_literal_message(org_fixture):
    result = await org_fixture.org({"name": "nonexistent-org-xyz"})
    text = result["content"][0]["text"]
    assert text.startswith("no outreach row matches 'nonexistent-org-xyz'")


async def test_org_missing_name_refuses(org_fixture):
    result = await org_fixture.org({"name": ""})
    text = result["content"][0]["text"]
    assert text.startswith("refused:")


async def test_org_found_wraps_free_text_and_leaves_status_plain(tmp_path, monkeypatch):
    outreach_dir = tmp_path / "outreach"
    _write_outreach_db(outreach_dir, [
        {
            "organisasjon": "Unique Firma", "gruppe": "G1", "kontaktperson": "Kari",
            "kontakt_epost": "kari@unique.no", "status": "Sendt",
            "varm_hook": "warm note", "notater": "some notes",
            # Real-DB shapes: scraped contact block, pasted job title,
            # pasted LinkedIn-source text, external URL — all third-party.
            "ekstra_kontakter": "Ola Extra <ola@unique.no> (scraped block)",
            "kontakt_rolle": "Head of Radical Hiring (pasted title)",
            "kontakt_kilde": "linkedin.com/in/kari — pasted profile snippet",
            "nettside": "https://unique-firma.example",
        },
    ])
    _patch_cfg(monkeypatch, {
        "outreach": outreach_dir,
        "job_search": tmp_path / "no-job-search",
        "prep": tmp_path / "no-prep",
    })
    from tools.jobhunt import handlers

    result = await handlers.org({"name": "Unique Firma"})
    data = result["data"]
    assert UNTRUSTED_MARK in data["organisasjon"]
    assert UNTRUSTED_MARK in data["kontaktperson"]
    assert UNTRUSTED_MARK in data["varm_hook"]
    assert UNTRUSTED_MARK in data["notater"]
    assert UNTRUSTED_MARK in data["ekstra_kontakter"]
    assert UNTRUSTED_MARK in data["kontakt_rolle"]
    assert UNTRUSTED_MARK in data["kontakt_kilde"]
    assert UNTRUSTED_MARK in data["nettside"]
    assert data["status"] == "Sendt"               # unwrapped structured field
    assert data["kontakt_epost"] == "kari@unique.no"  # emails stay bare (contact-emails decision)


# --------------------------------------------------------------------------
# jobhunt_prep
# --------------------------------------------------------------------------

async def test_prep_missing_folder_literal_message(prep_fixture):
    result = await prep_fixture.prep({"slug": "no-such-company"})
    text = result["content"][0]["text"]
    assert text == "no prep folder found for 'no-such-company'"


async def test_prep_missing_slug_refuses(prep_fixture):
    result = await prep_fixture.prep({"slug": ""})
    text = result["content"][0]["text"]
    assert text.startswith("refused:")


async def test_prep_found_reports_tier_files_and_confirmed_story_count(prep_fixture):
    result = await prep_fixture.prep({"slug": "test-co"})
    text = result["content"][0]["text"]
    data = result["data"]
    assert data["tier"] == "Tier: T0 — kickoff note"
    assert set(data["files_present"]) == {"company_dossier", "positioning", "interview_plan"}
    assert data["confirmed_story_count"] == 1      # only the CONFIRMED story counts
    assert "tier: Tier: T0" in text


async def test_prep_wraps_dossier_and_story_text(prep_fixture):
    result = await prep_fixture.prep({"slug": "test-co"})
    data = result["data"]
    assert UNTRUSTED_MARK in data["company_dossier"]
    assert UNTRUSTED_MARK in data["positioning"]
    assert UNTRUSTED_MARK in data["interview_plan"]
    assert UNTRUSTED_MARK in data["confirmed_stories"][0]
    text = result["content"][0]["text"]
    assert UNTRUSTED_MARK in text


# --------------------------------------------------------------------------
# ALL_TOOLS manifest
# --------------------------------------------------------------------------

def test_all_tools_has_exactly_four_entries():
    # Task 4 added jobhunt_draft_touch — the package's one write tool.
    from tools.jobhunt import ALL_TOOLS
    assert len(ALL_TOOLS) == 4
    names = {t.name for t in ALL_TOOLS}
    assert names == {"jobhunt_radar", "jobhunt_org", "jobhunt_prep", "jobhunt_draft_touch"}


# --------------------------------------------------------------------------
# static tools.yaml / catalog registration
# --------------------------------------------------------------------------

_TOOLS_YAML = yaml.safe_load(Path("config/tools.yaml").read_text(encoding="utf-8"))

_JOBHUNT_IDS = (
    "mcp__hikari_utility__jobhunt_radar",
    "mcp__hikari_utility__jobhunt_org",
    "mcp__hikari_utility__jobhunt_prep",
)


def test_tools_yaml_has_all_three_jobhunt_ids_untrusted():
    by_id = {t["id"]: t for t in _TOOLS_YAML["tools"]}
    for tid in _JOBHUNT_IDS:
        assert tid in by_id, f"{tid} missing from config/tools.yaml"
        entry = by_id[tid]
        assert entry["untrusted_output"] is True, f"{tid}: untrusted_output must be true"
        assert entry["server"] == "hikari_utility"
        assert entry["access_mode"] == "read"
        assert entry["gate"] is None


def test_catalog_has_keyword_lines_for_all_three():
    # Descriptions moved from catalog.py _ID_DESCRIPTIONS into tools.yaml
    # (sprint 3): the curated BM25 keyword line now lives on the yaml entry.
    by_id = {t["id"]: t for t in _TOOLS_YAML["tools"]}
    for tid in _JOBHUNT_IDS:
        desc = str(by_id[tid].get("description") or "")
        assert "job" in desc, f"{tid} missing a curated description keyword line"
