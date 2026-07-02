"""Tests for tools/jobhunt/drafter.py — jobhunt_draft_touch's compose ->
lint -> Gmail-draft pipeline (Sprint 2, Task 4).

Mocks ``run_internal_text`` (composition) and ``MANAGER.call`` (the Gmail
MCP, via ``patch("tools.jobhunt.drafter.MANAGER")`` — mirrors
tests/test_jobhunt_reply_radar.py's established pattern). The
``_block_live_mcp_calls`` autouse fixture in conftest.py already blocks
any unmocked ``McpManager.call``; these mocks patch the module-level
``MANAGER`` binding above that, per the fixture's own documented escape
hatch.
"""
from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agents import config as cfg
from tools.jobhunt import drafter

_ORG_COLUMNS = [
    "organisasjon", "gruppe", "kontaktperson", "kontakt_epost", "status",
    "varm_hook", "notater", "oppfolging_dato",
]


def _write_outreach_db(dir_: Path, rows: list[dict]) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(dir_ / "outreach.db")
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


def _patch_cfg(monkeypatch, **overrides):
    orig_get = cfg.get

    def fake_get(key, default=None):
        if key in overrides:
            return overrides[key]
        return orig_get(key, default)

    monkeypatch.setattr(cfg, "get", fake_get)


@pytest.fixture
def outreach_fixture(tmp_path, monkeypatch):
    outreach_dir = tmp_path / "outreach"
    job_search_dir = tmp_path / "job-search"  # deliberately not created -> cfg fallback
    _write_outreach_db(outreach_dir, [
        {
            "organisasjon": "Firma A", "gruppe": "G1", "kontaktperson": "Kari",
            "kontakt_epost": "kari@firma-a.no", "status": "Sendt",
            "varm_hook": "de lanserte et nytt prosjekt", "notater": "sendt CV 2026-06-01",
        },
        {
            "organisasjon": "Firma Mote", "gruppe": "G1", "kontaktperson": "Per",
            "kontakt_epost": "per@mote.no", "status": "Møte",
            "varm_hook": "", "notater": "",
        },
        {
            "organisasjon": "Firma Dod", "gruppe": "G1", "kontaktperson": "Ola",
            "kontakt_epost": "ola@dod.no", "status": "Død",
            "varm_hook": "", "notater": "",
        },
        {
            "organisasjon": "Firma Uten Epost", "gruppe": "G1", "kontaktperson": "Eva",
            "kontakt_epost": "", "status": "Sendt",
            "varm_hook": "", "notater": "",
        },
    ])
    _patch_cfg(monkeypatch, **{
        "jobhunt.roots.outreach": str(outreach_dir),
        "jobhunt.roots.job_search": str(job_search_dir),
    })
    return outreach_dir


_CLEAN_COMPOSE = (
    "SUBJECT: Kort oppfolging\n\n"
    "Hei Kari,\n\n"
    "Jeg leste om satsingen deres og lurer pa om det er rom for en kort prat.\n\n"
    "Mvh Oleksandr"
)

_DIRTY_COMPOSE = (
    "SUBJECT: Oppfolging;\n\n"
    "Jeg har B2+ niva i norsk."
)


def _fake_call_factory(*, draft_message_id="msg-1", verify_hits=True, draft_id="draft-1"):
    """Build an async fake for MANAGER.call routing on tool name."""

    async def fake_call(server, tool, args):
        if tool == "create_gmail_draft":
            return {"id": draft_id, "message": {"id": draft_message_id, "threadId": "t1"}}
        if tool == "query_gmail_emails":
            emails = (
                [{"id": draft_message_id, "threadId": "t1", "from": "me", "subject": "x"}]
                if verify_hits else []
            )
            return {"count": len(emails), "emails": emails}
        raise AssertionError(f"unexpected MCP call: {server}/{tool}")

    return fake_call


# --------------------------------------------------------------------------
# refusal paths — org lookup
# --------------------------------------------------------------------------

async def test_org_not_found_refuses(outreach_fixture, monkeypatch):
    mock_compose = AsyncMock(return_value=_CLEAN_COMPOSE)
    monkeypatch.setattr(drafter, "run_internal_text", mock_compose)
    with patch("tools.jobhunt.drafter.MANAGER") as mgr:
        mgr.call = AsyncMock()
        result = await drafter.draft_touch("nonexistent-xyz", "1")
    assert result["success"] is False
    assert "no outreach row matches" in result["text"]
    mock_compose.assert_not_called()
    mgr.call.assert_not_called()


async def test_ambiguous_org_refuses_with_candidate_list(tmp_path, monkeypatch):
    outreach_dir = tmp_path / "outreach"
    _write_outreach_db(outreach_dir, [
        {"organisasjon": "Firma A", "status": "Sendt"},
        {"organisasjon": "Firma AB", "status": "Sendt"},
    ])
    _patch_cfg(monkeypatch, **{"jobhunt.roots.outreach": str(outreach_dir)})
    mock_compose = AsyncMock(return_value=_CLEAN_COMPOSE)
    monkeypatch.setattr(drafter, "run_internal_text", mock_compose)
    with patch("tools.jobhunt.drafter.MANAGER") as mgr:
        mgr.call = AsyncMock()
        result = await drafter.draft_touch("Firma", "1")
    assert result["success"] is False
    assert "ambiguous" in result["data"]
    assert len(result["data"]["ambiguous"]) == 2
    mock_compose.assert_not_called()
    mgr.call.assert_not_called()


# --------------------------------------------------------------------------
# refusal paths — touch value / status gates
# --------------------------------------------------------------------------

async def test_touch_must_be_1_or_2(outreach_fixture, monkeypatch):
    mock_compose = AsyncMock(return_value=_CLEAN_COMPOSE)
    monkeypatch.setattr(drafter, "run_internal_text", mock_compose)
    result = await drafter.draft_touch("Firma A", "3")
    assert result["success"] is False
    assert "touch" in result["text"].lower()
    mock_compose.assert_not_called()


async def test_mote_status_refuses_hand_written_only(outreach_fixture, monkeypatch):
    mock_compose = AsyncMock(return_value=_CLEAN_COMPOSE)
    monkeypatch.setattr(drafter, "run_internal_text", mock_compose)
    with patch("tools.jobhunt.drafter.MANAGER") as mgr:
        mgr.call = AsyncMock()
        result = await drafter.draft_touch("Firma Mote", "1")
    assert result["success"] is False
    assert "hand-written only" in result["text"]
    mock_compose.assert_not_called()
    mgr.call.assert_not_called()


async def test_dod_status_refuses_and_mentions_t90(outreach_fixture, monkeypatch):
    mock_compose = AsyncMock(return_value=_CLEAN_COMPOSE)
    monkeypatch.setattr(drafter, "run_internal_text", mock_compose)
    with patch("tools.jobhunt.drafter.MANAGER") as mgr:
        mgr.call = AsyncMock()
        result = await drafter.draft_touch("Firma Dod", "1")
    assert result["success"] is False
    assert "T+90" in result["text"]
    assert "new hook" in result["text"].lower()
    mock_compose.assert_not_called()
    mgr.call.assert_not_called()


async def test_missing_kontakt_epost_refuses(outreach_fixture, monkeypatch):
    mock_compose = AsyncMock(return_value=_CLEAN_COMPOSE)
    monkeypatch.setattr(drafter, "run_internal_text", mock_compose)
    with patch("tools.jobhunt.drafter.MANAGER") as mgr:
        mgr.call = AsyncMock()
        result = await drafter.draft_touch("Firma Uten Epost", "1")
    assert result["success"] is False
    assert "kontakt_epost" in result["text"].lower()
    mock_compose.assert_not_called()
    mgr.call.assert_not_called()


# --------------------------------------------------------------------------
# rails-fail path — never calls draft-create
# --------------------------------------------------------------------------

async def test_rails_fail_never_calls_draft_create(outreach_fixture, monkeypatch):
    mock_compose = AsyncMock(return_value=_DIRTY_COMPOSE)
    monkeypatch.setattr(drafter, "run_internal_text", mock_compose)
    with patch("tools.jobhunt.drafter.MANAGER") as mgr:
        mgr.call = AsyncMock()
        result = await drafter.draft_touch("Firma A", "1")
    assert result["success"] is False
    assert "RAILS FAILED" in result["text"]
    assert "not drafted" in result["text"]
    mgr.call.assert_not_called()
    # initial compose + exactly one recompose attempt, never more.
    assert mock_compose.await_count == 2


async def test_rails_fail_text_lists_the_lint_hits(outreach_fixture, monkeypatch):
    monkeypatch.setattr(drafter, "run_internal_text", AsyncMock(return_value=_DIRTY_COMPOSE))
    with patch("tools.jobhunt.drafter.MANAGER") as mgr:
        mgr.call = AsyncMock()
        result = await drafter.draft_touch("Firma A", "1")
    assert "semicolon" in result["text"].lower()
    assert "b2+" in result["text"].lower()
    assert result["data"]["lint_hits"]


async def test_recompose_after_lint_hit_then_passes(outreach_fixture, monkeypatch):
    """A dirty first draft that recomposes clean must still succeed —
    ONE recompose is allowed before giving up."""
    responses = [_DIRTY_COMPOSE, _CLEAN_COMPOSE]

    async def fake_compose(prompt, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(drafter, "run_internal_text", fake_compose)
    with patch("tools.jobhunt.drafter.MANAGER") as mgr:
        mgr.call = AsyncMock(side_effect=_fake_call_factory())
        result = await drafter.draft_touch("Firma A", "1")
    assert result["success"] is True
    assert responses == []  # both queued responses were consumed


# --------------------------------------------------------------------------
# happy path — creates once, verifies, reports success
# --------------------------------------------------------------------------

async def test_happy_path_creates_once_and_verifies(outreach_fixture, monkeypatch):
    monkeypatch.setattr(drafter, "run_internal_text", AsyncMock(return_value=_CLEAN_COMPOSE))
    with patch("tools.jobhunt.drafter.MANAGER") as mgr:
        mgr.call = AsyncMock(side_effect=_fake_call_factory())
        result = await drafter.draft_touch("Firma A", "1")

    assert result["success"] is True
    assert "kari@firma-a.no" in result["text"]
    assert "Kort oppfolging" in result["text"]
    assert "notater" in result["text"].lower()
    assert "sendt CV 2026-06-01" in result["text"]

    create_calls = [c for c in mgr.call.call_args_list if c.args[1] == "create_gmail_draft"]
    verify_calls = [c for c in mgr.call.call_args_list if c.args[1] == "query_gmail_emails"]
    assert len(create_calls) == 1
    assert len(verify_calls) == 1
    # the recipient/subject/body actually reached the MCP call
    assert create_calls[0].args[2]["to"] == "kari@firma-a.no"
    assert create_calls[0].args[2]["subject"] == "Kort oppfolging"


async def test_happy_path_data_payload_has_draft_id(outreach_fixture, monkeypatch):
    monkeypatch.setattr(drafter, "run_internal_text", AsyncMock(return_value=_CLEAN_COMPOSE))
    with patch("tools.jobhunt.drafter.MANAGER") as mgr:
        mgr.call = AsyncMock(side_effect=_fake_call_factory(draft_id="draft-xyz"))
        result = await drafter.draft_touch("Firma A", "1")
    assert result["data"]["draft_id"] == "draft-xyz"


# --------------------------------------------------------------------------
# verify-miss — never reports success
# --------------------------------------------------------------------------

async def test_verify_miss_returns_failure_not_success(outreach_fixture, monkeypatch):
    monkeypatch.setattr(drafter, "run_internal_text", AsyncMock(return_value=_CLEAN_COMPOSE))
    with patch("tools.jobhunt.drafter.MANAGER") as mgr:
        mgr.call = AsyncMock(side_effect=_fake_call_factory(verify_hits=False))
        result = await drafter.draft_touch("Firma A", "1")

    assert result["success"] is False
    assert "could not be verified" in result["text"]
    assert "check gmail manually" in result["text"]


async def test_create_draft_mcp_error_returns_failure_not_success(outreach_fixture, monkeypatch):
    from agents.mcp_manager import McpCallError

    monkeypatch.setattr(drafter, "run_internal_text", AsyncMock(return_value=_CLEAN_COMPOSE))
    with patch("tools.jobhunt.drafter.MANAGER") as mgr:
        mgr.call = AsyncMock(
            side_effect=McpCallError("google_workspace", "create_gmail_draft", "boom")
        )
        result = await drafter.draft_touch("Firma A", "1")
    assert result["success"] is False
    assert "check gmail manually" in result["text"]


async def test_compose_returns_empty_string_refuses_without_drafting(outreach_fixture, monkeypatch):
    monkeypatch.setattr(drafter, "run_internal_text", AsyncMock(return_value=""))
    with patch("tools.jobhunt.drafter.MANAGER") as mgr:
        mgr.call = AsyncMock()
        result = await drafter.draft_touch("Firma A", "1")
    assert result["success"] is False
    mgr.call.assert_not_called()


# --------------------------------------------------------------------------
# terminal lint gate — the EXACT final subject+body strings are re-linted
# --------------------------------------------------------------------------

async def test_fallback_subject_with_banned_org_token_never_drafts(tmp_path, monkeypatch):
    """If the LLM omits the 'SUBJECT:' line, the fallback subject is
    synthesized from the org name — which can carry banned tokens the
    compose-time lint (run on the LLM output only) never saw. The terminal
    gate must re-lint the EXACT final subject+body and block the draft."""
    outreach_dir = tmp_path / "outreach"
    _write_outreach_db(outreach_dir, [
        {
            "organisasjon": "Acme; 2027", "gruppe": "G1", "kontaktperson": "Kari",
            "kontakt_epost": "kari@acme.no", "status": "Sendt",
            "varm_hook": "", "notater": "",
        },
    ])
    _patch_cfg(monkeypatch, **{
        "jobhunt.roots.outreach": str(outreach_dir),
        "jobhunt.roots.job_search": str(tmp_path / "no-job-search"),
    })
    # Clean bokmal body, but NO 'SUBJECT:' first line -> the fallback subject
    # 'Oppfolging — Acme; 2027' carries a semicolon + the banned year.
    body_only = (
        "Hei Kari,\n\n"
        "Jeg leste om satsingen deres og lurer pa om det er rom for en kort prat.\n\n"
        "Mvh Oleksandr"
    )
    monkeypatch.setattr(drafter, "run_internal_text", AsyncMock(return_value=body_only))
    with patch("tools.jobhunt.drafter.MANAGER") as mgr:
        mgr.call = AsyncMock(side_effect=_fake_call_factory())
        result = await drafter.draft_touch("Acme", "1")

    assert result["success"] is False
    assert "RAILS FAILED" in result["text"]
    assert "not drafted" in result["text"]
    assert result["data"]["lint_hits"]
    mgr.call.assert_not_called()


# --------------------------------------------------------------------------
# architecture guards
# --------------------------------------------------------------------------

def test_drafter_no_direct_db_access_single_compose_call_site():
    """No direct sqlite access (must go through readers.org_context), and
    exactly one call SITE for run_internal_text (composition logic lives
    in one place, reused for the initial compose + the one recompose)."""
    code_src = "\n".join(
        inspect.getsource(obj) for _name, obj in vars(drafter).items()
        if inspect.isfunction(obj) and obj.__module__ == drafter.__name__
    )
    assert "sqlite3" not in code_src
    assert "outreach.db" not in code_src
    assert "job_search.db" not in code_src
    assert code_src.count("run_internal_text(") == 1
    assert "MANAGER.call(" in code_src
