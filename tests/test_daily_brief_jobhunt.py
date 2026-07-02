"""Tests for agents/daily_brief.py — the ``jobhunt`` section (Sprint 2,
Task 3): collector wiring (``_collect_jobhunt`` / ``collect_sections``) and
the composer block.

Mirrors the ``fresh_db`` fixture pattern from tests/test_daily_brief_collect.py
and the cfg-monkeypatch pattern from tests/test_jobhunt_readers.py.
"""
from __future__ import annotations

import importlib
import inspect
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from agents import daily_brief
from storage import db

TODAY = date(2026, 7, 2)

_ORG_COLUMNS = [
    "notion_page_id", "organisasjon", "gruppe", "kommune", "nettside",
    "kontaktperson", "kontakt_epost", "kontakt_kilde", "kontakt_rolle",
    "kontakt_hiring", "ekstra_kontakter", "tar_apen_soknad", "varm_hook",
    "fit_score", "reachability", "status", "cv_variant", "sendt_dato",
    "oppfolging_dato", "oppfolging2_dato", "reengasjement_dato", "notater",
    "opprettet",
]


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield db
    db._reset_schema_sentinel()


@pytest.fixture()
def _no_weather_email_calendar(monkeypatch):
    """Neutralizes the other three collectors exactly like
    tests/test_daily_brief_collect.py does, so each test here isolates the
    jobhunt path."""
    async def no_email():
        return {"unread_personal": [], "calendar_invites": [],
                "deletable": {"count": 0, "top_senders": [], "sample_ids": []}}

    async def no_events():
        return []

    monkeypatch.setattr(daily_brief, "fetch_email_buckets", no_email)
    monkeypatch.setattr(daily_brief, "fetch_calendar_events", no_events)
    monkeypatch.setattr(daily_brief, "_resolve_location", lambda: None)


def _patch_cfg(monkeypatch, **overrides):
    from agents import config as cfg
    orig_get = cfg.get

    def fake_get(key, default=None):
        if key in overrides:
            return overrides[key]
        return orig_get(key, default)

    monkeypatch.setattr(cfg, "get", fake_get)


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


async def _no_replies(_today):
    return []


# --------------------------------------------------------------------------
# gate: jobhunt.enabled
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_collect_jobhunt_none_when_disabled(fresh_db, monkeypatch, _no_weather_email_calendar):
    _patch_cfg(monkeypatch, **{"jobhunt.enabled": False})

    def _boom(*a, **kw):
        raise AssertionError("readers must not be called when jobhunt.enabled=False")

    monkeypatch.setattr(daily_brief.jobhunt_readers, "outreach_due", _boom)
    monkeypatch.setattr(daily_brief.jobhunt_readers, "application_deadlines", _boom)
    monkeypatch.setattr(daily_brief.jobhunt_readers, "interviews_upcoming", _boom)

    async def _boom_async(_today):
        raise AssertionError("reply_radar must not run when jobhunt.enabled=False")

    monkeypatch.setattr(daily_brief.reply_radar, "scan", _boom_async)

    sections = await daily_brief.collect_sections()
    assert sections["jobhunt"] is None


# --------------------------------------------------------------------------
# empty everywhere -> None
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_collect_jobhunt_none_when_all_readers_empty(fresh_db, monkeypatch, _no_weather_email_calendar):
    monkeypatch.setattr(daily_brief.jobhunt_readers, "outreach_due", lambda today: [])
    monkeypatch.setattr(daily_brief.jobhunt_readers, "application_deadlines", lambda today: [])
    monkeypatch.setattr(daily_brief.jobhunt_readers, "interviews_upcoming", lambda today: [])
    monkeypatch.setattr(daily_brief.reply_radar, "scan", _no_replies)

    sections = await daily_brief.collect_sections()
    assert sections["jobhunt"] is None
    # existing sections stay None too — the jobhunt wiring must not leak signal
    assert sections["email"] is None
    assert sections["calendar"] is None
    assert sections["weather"] is None


@pytest.mark.asyncio
async def test_collect_jobhunt_none_when_roots_missing(fresh_db, monkeypatch, tmp_path,
                                                         _no_weather_email_calendar):
    """Real readers.* functions (not monkeypatched) against roots that don't
    exist on disk — the contract's exact 'roots are missing' scenario."""
    _patch_cfg(monkeypatch, **{
        "jobhunt.roots.outreach": str(tmp_path / "no-outreach"),
        "jobhunt.roots.job_search": str(tmp_path / "no-job-search"),
        "jobhunt.roots.prep": str(tmp_path / "no-prep"),
    })
    monkeypatch.setattr(daily_brief.reply_radar, "scan", _no_replies)

    sections = await daily_brief.collect_sections()
    assert sections["jobhunt"] is None


# --------------------------------------------------------------------------
# populated + capped
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_collect_jobhunt_populated_and_capped_top_3(fresh_db, monkeypatch, _no_weather_email_calendar):
    due = [{"org": f"Org{i}", "kontakt": "K", "touch": "1", "due": "2026-07-01",
            "days_overdue": i, "varm_hook": "", "notater_tail": "", "epost": "", "gruppe": ""}
           for i in range(5)]
    deadlines = [{"org": f"DOrg{i}", "stilling": "Role", "frist": "2026-07-05",
                  "next_action": ""} for i in range(5)]
    interviews = [{"org": f"IOrg{i}", "slug": f"i{i}", "date": "2026-07-06",
                   "source": "prep", "prep_state": "Prepped"} for i in range(5)]
    replies = [{"from": f"c{i}@x.no", "org_or_employer": f"ROrg{i}",
                "subject": "hi", "gmail_thread_id": f"t{i}", "message_id": f"m{i}"}
               for i in range(5)]

    monkeypatch.setattr(daily_brief.jobhunt_readers, "outreach_due", lambda today: due)
    monkeypatch.setattr(daily_brief.jobhunt_readers, "application_deadlines", lambda today: deadlines)
    monkeypatch.setattr(daily_brief.jobhunt_readers, "interviews_upcoming", lambda today: interviews)

    async def _replies(_today):
        return replies
    monkeypatch.setattr(daily_brief.reply_radar, "scan", _replies)

    sections = await daily_brief.collect_sections()
    jh = sections["jobhunt"]
    assert jh is not None
    assert len(jh["due_touches"]) == 3
    assert len(jh["deadlines"]) == 3
    assert len(jh["interviews"]) == 3
    assert len(jh["replies"]) == 3
    # cap preserves original (earliest/most-relevant-first) ordering
    assert [e["org"] for e in jh["due_touches"]] == ["Org0", "Org1", "Org2"]


# --------------------------------------------------------------------------
# constraint re-check at section level: Møte rows never surface
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_collect_jobhunt_excludes_mote_row_at_section_level(fresh_db, monkeypatch, tmp_path,
                                                                    _no_weather_email_calendar):
    outreach_dir = tmp_path / "outreach"
    _write_outreach_db(outreach_dir, [
        {  # Sendt, due 7 days ago -> surfaces
            "organisasjon": "Firma A", "status": "Sendt",
            "oppfolging_dato": "2026-06-25", "kontaktperson": "Kari",
        },
        {  # Møte (warm) with a date that WOULD be due -> must never surface
            "organisasjon": "Firma B", "status": "Møte",
            "oppfolging_dato": "2026-06-25",
        },
    ])
    _patch_cfg(monkeypatch, **{
        "jobhunt.roots.outreach": str(outreach_dir),
        "jobhunt.roots.job_search": str(tmp_path / "no-job-search"),
        "jobhunt.roots.prep": str(tmp_path / "no-prep"),
    })
    monkeypatch.setattr(daily_brief.reply_radar, "scan", _no_replies)

    sections = await daily_brief.collect_sections()
    jh = sections["jobhunt"]
    assert jh is not None
    orgs = [e["org"] for e in jh["due_touches"]]
    assert "Firma A" in orgs
    assert "Firma B" not in orgs


# --------------------------------------------------------------------------
# composer
# --------------------------------------------------------------------------

def _jobhunt_sections():
    return {
        "weather": None, "email": None, "calendar": None,
        "jobhunt": {
            "due_touches": [{"org": "Firma A", "kontakt": "Kari", "touch": "1",
                              "due": "2026-06-25", "days_overdue": 7,
                              "varm_hook": "", "notater_tail": "", "epost": "", "gruppe": ""}],
            "deadlines": [{"org": "Acme AS", "stilling": "Engineer",
                            "frist": "2026-07-05", "next_action": ""}],
            "interviews": [{"org": "Test Co", "slug": "test-co", "date": "2026-07-10",
                             "source": "prep", "prep_state": "Prepped"}],
            "replies": [{"from": "kari@firma-a.no", "org_or_employer": "Firma A",
                         "subject": "re: your outreach", "gmail_thread_id": "t1",
                         "message_id": "m1"}],
        },
    }


def test_compose_prompt_jobhunt_next_actions_present():
    prompt = daily_brief.compose_prompt(_jobhunt_sections())
    assert prompt is not None
    assert "draft touch" in prompt
    assert "apply?" in prompt
    assert "want the prep brief?" in prompt
    assert "want me to pull up the thread?" in prompt


def test_compose_prompt_jobhunt_wraps_untrusted_content():
    prompt = daily_brief.compose_prompt(_jobhunt_sections())
    assert "HIKARI_UNTRUSTED" in prompt
    assert "mcp__hikari_utility__jobhunt_radar" in prompt


def test_compose_prompt_jobhunt_never_suggests_application_followup():
    """CRITICAL constraint (2026-06-25 rule): deadlines get 'apply?', outreach
    touches get 'draft touch N?', interviews get 'want the prep brief?' — a
    SUBMITTED application must never get a follow-up/nudge suggestion."""
    prompt = daily_brief.compose_prompt(_jobhunt_sections())
    assert "follow up on your application" not in prompt
    assert "nudge" not in prompt


def test_composer_template_source_never_suggests_application_followup():
    """Static guard on the template SOURCE itself (not just one rendered
    example) — these literal phrases must never appear anywhere in
    agents/daily_brief.py, so no future edit can reintroduce them."""
    src = inspect.getsource(daily_brief)
    assert "follow up on your application" not in src
    assert "nudge" not in src


def test_compose_prompt_missing_jobhunt_key_is_backward_compatible():
    """Pre-Sprint-2 sections dicts (no 'jobhunt' key at all) must keep
    working — collect_sections() always sets the key, but compose_prompt
    must not KeyError if some other caller omits it."""
    assert daily_brief.compose_prompt(
        {"weather": None, "email": None, "calendar": None}) is None
