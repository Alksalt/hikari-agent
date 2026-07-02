"""Tests for tools/jobhunt/reply_radar.py — Gmail reply radar + the
append-only handoff writer (Sprint 2, Task 3).

Mirrors tests/test_typed_gmail_adapter.py's MANAGER.call mocking pattern
(``patch("tools.jobhunt.reply_radar.MANAGER")`` + ``AsyncMock``).
``readers.contact_emails()`` is monkeypatched directly rather than built
from real sqlite fixtures — that extraction logic is already covered by
tests/test_jobhunt_readers.py; this file tests reply_radar's OWN logic
(matching, dedup, handoff write, verify-after-write).
"""
from __future__ import annotations

import importlib
import inspect
import logging
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agents.mcp_manager import McpCallError
from tools.jobhunt import readers, reply_radar

TODAY = date(2026, 7, 2)
FIXED_NOW = datetime(2026, 7, 2, 8, 30)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from agents import config as cfg
    cfg.reload()
    yield


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(reply_radar, "_now_local", lambda: FIXED_NOW)


def _patch_cfg(monkeypatch, **overrides):
    """Mirrors tests/test_jobhunt_readers.py::_patch_cfg — fakes cfg.get for
    just the keys reply_radar cares about, falling through to the real
    loader for everything else."""
    from agents import config as cfg
    orig_get = cfg.get

    def fake_get(key, default=None):
        if key in overrides:
            return overrides[key]
        return orig_get(key, default)

    monkeypatch.setattr(cfg, "get", fake_get)


def _sample_response(extra=None):
    emails = [
        {"id": "m1", "threadId": "t1", "from": "Kari <kari@firma-a.no>",
         "subject": "re: your outreach", "snippet": "hi"},
        {"id": "m2", "threadId": "t2", "from": "stranger@nowhere.com",
         "subject": "unrelated spam"},
    ]
    if extra:
        emails.extend(extra)
    return {"count": len(emails), "emails": emails}


# --------------------------------------------------------------------------
# small unit helpers
# --------------------------------------------------------------------------

def test_extract_address_both_forms():
    assert reply_radar._extract_address("kari@firma-a.no") == "kari@firma-a.no"
    assert reply_radar._extract_address("Kari <Kari@Firma-A.NO>") == "kari@firma-a.no"
    assert reply_radar._extract_address("garbage-no-at") == ""


def test_coerce_reply_prefers_gmail_id_over_header_message_id():
    """Live shape carries BOTH a top-level `id` (Gmail's own id) and a
    header-derived `message_id` (RFC822 Message-ID) with a DIFFERENT value.
    We must use Gmail's own id, matching GmailMessage.id in inbox.py."""
    raw = {"id": "gmail-id-1", "message_id": "<rfc822-header-value@mail.gmail.com>",
           "threadId": "thread-1", "from": "a@b.com", "subject": "hi"}
    coerced = reply_radar._coerce_reply(raw)
    assert coerced["id"] == "gmail-id-1"
    assert coerced["thread_id"] == "thread-1"


def test_employer_label_from_domain():
    assert reply_radar._employer_label("firma-a.no") == "Firma A"
    assert reply_radar._employer_label("") == "(unknown)"


def test_extract_messages_text_json_fallback():
    import json
    wrapped = {"text": json.dumps(_sample_response())}
    assert len(reply_radar._extract_messages(wrapped)) == 2


# --------------------------------------------------------------------------
# scan() — contact gate short-circuits before any MCP call
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_returns_empty_and_skips_mcp_when_no_contacts(monkeypatch):
    monkeypatch.setattr(readers, "contact_emails", lambda: set())
    with patch("tools.jobhunt.reply_radar.MANAGER") as mgr:
        mgr.call = AsyncMock(return_value=_sample_response())
        out = await reply_radar.scan(TODAY)
    assert out == []
    mgr.call.assert_not_called()


# --------------------------------------------------------------------------
# scan() — match + write + verify happy path
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_matches_known_contact_and_writes_handoff(tmp_path, monkeypatch):
    monkeypatch.setattr(readers, "contact_emails", lambda: {"kari@firma-a.no"})
    outreach_dir = tmp_path / "outreach"
    outreach_dir.mkdir()
    _patch_cfg(monkeypatch, **{
        "jobhunt.roots.outreach": str(outreach_dir),
        "jobhunt.handoff_file": "hikari_inbox.md",
        "jobhunt.reply_lookback_days": 2,
    })

    with patch("tools.jobhunt.reply_radar.MANAGER") as mgr:
        mgr.call = AsyncMock(return_value=_sample_response())
        out = await reply_radar.scan(TODAY)

    # Only the known contact (kari@firma-a.no) matches — the stranger is dropped.
    assert len(out) == 1
    reply = out[0]
    assert reply["from"] == "kari@firma-a.no"
    assert reply["org_or_employer"] == "Firma A"
    assert reply["subject"] == "re: your outreach"
    assert reply["gmail_thread_id"] == "t1"
    assert reply["message_id"] == "m1"

    handoff = outreach_dir / "hikari_inbox.md"
    assert handoff.is_file()
    text = handoff.read_text(encoding="utf-8")
    assert "append-only handoff written by hikari" in text
    assert "do not hand-edit lines, mark them processed instead" in text
    assert (
        "reply from kari@firma-a.no (Firma A) — subject: re: your outreach "
        "— thread:t1 — msg:m1 — status: unprocessed"
    ) in text
    assert "2026-07-02 08:30" in text
    # The stranger's message must never be logged.
    assert "stranger@nowhere.com" not in text


@pytest.mark.asyncio
async def test_scan_query_uses_configured_lookback_days(tmp_path, monkeypatch):
    monkeypatch.setattr(readers, "contact_emails", lambda: {"kari@firma-a.no"})
    outreach_dir = tmp_path / "outreach"
    outreach_dir.mkdir()
    _patch_cfg(monkeypatch, **{
        "jobhunt.roots.outreach": str(outreach_dir),
        "jobhunt.reply_lookback_days": 5,
    })
    with patch("tools.jobhunt.reply_radar.MANAGER") as mgr:
        mgr.call = AsyncMock(return_value=_sample_response())
        await reply_radar.scan(TODAY)
    args, kwargs = mgr.call.call_args
    query = args[2]["query"] if len(args) > 2 else kwargs["arguments"]["query"]
    assert "newer_than:5d" in query


# --------------------------------------------------------------------------
# dedup
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_dedup_second_scan_appends_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(readers, "contact_emails", lambda: {"kari@firma-a.no"})
    outreach_dir = tmp_path / "outreach"
    outreach_dir.mkdir()
    _patch_cfg(monkeypatch, **{"jobhunt.roots.outreach": str(outreach_dir)})

    with patch("tools.jobhunt.reply_radar.MANAGER") as mgr:
        mgr.call = AsyncMock(return_value=_sample_response())
        first = await reply_radar.scan(TODAY)
    assert len(first) == 1

    handoff = outreach_dir / "hikari_inbox.md"
    content_after_first = handoff.read_text(encoding="utf-8")

    with patch("tools.jobhunt.reply_radar.MANAGER") as mgr:
        mgr.call = AsyncMock(return_value=_sample_response())
        second = await reply_radar.scan(TODAY)

    assert second == []  # nothing NEW surfaced
    assert handoff.read_text(encoding="utf-8") == content_after_first  # file unchanged


@pytest.mark.asyncio
async def test_scan_header_written_only_once(tmp_path, monkeypatch):
    monkeypatch.setattr(readers, "contact_emails",
                         lambda: {"kari@firma-a.no", "per@firma-d.no"})
    outreach_dir = tmp_path / "outreach"
    outreach_dir.mkdir()
    _patch_cfg(monkeypatch, **{"jobhunt.roots.outreach": str(outreach_dir)})

    with patch("tools.jobhunt.reply_radar.MANAGER") as mgr:
        mgr.call = AsyncMock(return_value=_sample_response())
        await reply_radar.scan(TODAY)

    with patch("tools.jobhunt.reply_radar.MANAGER") as mgr:
        mgr.call = AsyncMock(return_value=_sample_response(extra=[
            {"id": "m3", "threadId": "t3", "from": "per@firma-d.no",
             "subject": "another reply"},
        ]))
        second = await reply_radar.scan(TODAY)

    assert len(second) == 1
    assert second[0]["message_id"] == "m3"
    text = (outreach_dir / "hikari_inbox.md").read_text(encoding="utf-8")
    assert text.count("append-only handoff written by hikari") == 1


# --------------------------------------------------------------------------
# verify-after-write failure path
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_verify_after_write_mismatch_excludes_reply(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(readers, "contact_emails", lambda: {"kari@firma-a.no"})
    outreach_dir = tmp_path / "outreach"
    outreach_dir.mkdir()
    _patch_cfg(monkeypatch, **{"jobhunt.roots.outreach": str(outreach_dir)})

    # Simulate a corrupted/short re-read: the write itself happens for real
    # (Path.open("a") is untouched), but the verify-step's read_text call
    # sees content that does NOT contain the line we just wrote.
    monkeypatch.setattr(Path, "read_text", lambda self, encoding="utf-8": "corrupted\n")

    with caplog.at_level(logging.ERROR):
        with patch("tools.jobhunt.reply_radar.MANAGER") as mgr:
            mgr.call = AsyncMock(return_value=_sample_response())
            out = await reply_radar.scan(TODAY)

    assert out == []  # never surfaced as logged when verify failed
    assert any("verify-after-write mismatch" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_scan_handoff_write_failure_excludes_all(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(readers, "contact_emails", lambda: {"kari@firma-a.no"})
    outreach_dir = tmp_path / "outreach"
    outreach_dir.mkdir()
    _patch_cfg(monkeypatch, **{"jobhunt.roots.outreach": str(outreach_dir)})

    orig_open = Path.open

    def _boom_on_append(self, mode="r", *args, **kwargs):
        if mode == "a":
            raise OSError("disk full")
        return orig_open(self, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", _boom_on_append)

    with caplog.at_level(logging.ERROR):
        with patch("tools.jobhunt.reply_radar.MANAGER") as mgr:
            mgr.call = AsyncMock(return_value=_sample_response())
            out = await reply_radar.scan(TODAY)

    assert out == []
    assert any("handoff write failed" in r.message for r in caplog.records)


# --------------------------------------------------------------------------
# failure isolation — gmail/mcp errors never raise
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_mcp_call_error_returns_empty_list(tmp_path, monkeypatch):
    monkeypatch.setattr(readers, "contact_emails", lambda: {"kari@firma-a.no"})
    _patch_cfg(monkeypatch, **{"jobhunt.roots.outreach": str(tmp_path)})
    with patch("tools.jobhunt.reply_radar.MANAGER") as mgr:
        mgr.call = AsyncMock(
            side_effect=McpCallError("google_workspace", "query_gmail_emails", "boom"))
        out = await reply_radar.scan(TODAY)
    assert out == []


@pytest.mark.asyncio
async def test_scan_unexpected_spawn_exception_returns_empty_list(tmp_path, monkeypatch):
    """A subprocess spawn failure raises a bare exception (not McpCallError) —
    reply_radar must catch broadly, never propagate, never block the brief."""
    monkeypatch.setattr(readers, "contact_emails", lambda: {"kari@firma-a.no"})
    _patch_cfg(monkeypatch, **{"jobhunt.roots.outreach": str(tmp_path)})
    with patch("tools.jobhunt.reply_radar.MANAGER") as mgr:
        mgr.call = AsyncMock(side_effect=RuntimeError("subprocess spawn failed"))
        out = await reply_radar.scan(TODAY)
    assert out == []


# --------------------------------------------------------------------------
# missing outreach root — matches found but cannot be logged
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_drops_matches_when_outreach_root_missing(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(readers, "contact_emails", lambda: {"kari@firma-a.no"})
    _patch_cfg(monkeypatch, **{"jobhunt.roots.outreach": str(tmp_path / "does-not-exist")})

    with caplog.at_level(logging.WARNING):
        with patch("tools.jobhunt.reply_radar.MANAGER") as mgr:
            mgr.call = AsyncMock(return_value=_sample_response())
            out = await reply_radar.scan(TODAY)

    assert out == []
    assert any("cannot be logged" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_scan_drops_matches_when_outreach_root_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setattr(readers, "contact_emails", lambda: {"kari@firma-a.no"})
    _patch_cfg(monkeypatch, **{"jobhunt.roots.outreach": ""})
    with patch("tools.jobhunt.reply_radar.MANAGER") as mgr:
        mgr.call = AsyncMock(return_value=_sample_response())
        out = await reply_radar.scan(TODAY)
    assert out == []


# --------------------------------------------------------------------------
# architecture guards
# --------------------------------------------------------------------------

def test_reply_radar_no_llm_and_no_direct_db_access():
    """No LLM delegation (typed-adapter provenance) AND no direct sqlite
    access — reply_radar reads outreach.db/job_search.db ONLY through
    readers.contact_emails(), never opens either database itself.

    Scans function bodies only (not the module docstring, which legitimately
    names outreach.db/job_search.db/Notion while documenting the contract)."""
    code_src = "\n".join(
        inspect.getsource(obj) for _name, obj in vars(reply_radar).items()
        if inspect.isfunction(obj) and obj.__module__ == reply_radar.__name__
    )
    assert "run_internal_control" not in code_src
    assert "run_internal_text" not in code_src
    assert "sqlite3" not in code_src
    assert "outreach.db" not in code_src
    assert "job_search.db" not in code_src
    assert "notion" not in code_src.lower()
    assert "MANAGER.call(" in code_src
