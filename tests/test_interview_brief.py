"""Tests for agents/interview_brief.py — evening-before interview-prep
briefing producer (Sprint 2, Task 5).

Mirrors the orchestration-mocking style of tests/test_daily_brief_send.py
(fresh_db fixture) and tests/test_ceremony_tg_id_propagation.py (_gate_open
fixture that forces the proactive_gate's silence/quiet checks open so the
cadence + dedup marker logic under test isn't incidentally blocked by
wall-clock quiet hours).
"""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta

import pytest

from agents import interview_brief
from storage import db


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("HOME_TZ", "Europe/Berlin")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield db
    db._reset_schema_sentinel()


@pytest.fixture()
def gate_open(monkeypatch):
    """Force proactive_gate's silence/quiet-hours checks open so cadence +
    dedup-marker behavior can be tested in isolation."""
    import agents.proactive_gate as _gate
    monkeypatch.setattr(_gate, "_is_quiet_now", lambda _db=None: False)
    monkeypatch.setattr(_gate, "_silence_active", lambda _db: False)


@pytest.fixture()
def cadence_allow(monkeypatch):
    from agents import cadence
    monkeypatch.setattr(cadence, "can_send", lambda source, pool=None: (True, "ok"))
    monkeypatch.setattr(cadence, "record_ceremony_sent", lambda source: None)


def _entry(org="Acme", slug="acme", when="2026-07-04", source="prep", prep_state="Screening"):
    return {"org": org, "slug": slug, "date": when, "source": source, "prep_state": prep_state}


# ---------- compose_prompt: degraded-honest path ----------

def test_compose_prompt_degraded_when_no_prep_files():
    entry = _entry()
    prompt = interview_brief.compose_prompt(entry, {})
    low = prompt.lower()
    assert "no prep folder" in low
    assert "acme" in low
    assert "/prep acme" in low or "prep acme" in low
    # never fabricate — no dossier/story markers should appear
    assert "confirmed" not in low
    assert "positioning excerpt" not in low


def test_compose_prompt_degraded_when_prep_files_have_no_substantive_content():
    """A prep dict that only has tier/confirmed_stories (no dossier/positioning/
    plan) still counts as 'no prep folder' for the interview itself."""
    entry = _entry()
    prep = {"tier": "Tier 1", "confirmed_stories": []}
    prompt = interview_brief.compose_prompt(entry, prep)
    assert "no prep folder" in prompt.lower()


# ---------- compose_prompt: full prep path ----------

def test_compose_prompt_full_includes_wrapped_prep_content():
    entry = _entry()
    prep = {
        "tier": "Tier 1 — high priority",
        "positioning": "lead with backend depth and health-tech interest.",
        "interview_plan": "Q: why this company? Q: describe a hard bug you fixed.",
        "confirmed_stories": [
            "### 1. The migration\n> Confirmed: 2026-07-01\nfixed prod at 2am.",
            "### 2. The outage\n> Confirmed: 2026-06-20\ncalm under fire.",
        ],
    }
    prompt = interview_brief.compose_prompt(entry, prep)
    assert "HIKARI_UNTRUSTED" in prompt
    assert "Tier 1" in prompt
    assert "backend depth" in prompt
    assert "hard bug you fixed" in prompt
    assert "the migration" in prompt.lower()
    assert "no prep folder" not in prompt.lower()


def test_compose_prompt_caps_prep_text(monkeypatch):
    from agents import config as cfg
    monkeypatch.setattr(
        cfg, "get",
        lambda key, default=None: 20 if key == "jobhunt.prep_file_char_cap" else default,
    )
    entry = _entry()
    prep = {"positioning": "x" * 500, "interview_plan": "y" * 500}
    prompt = interview_brief.compose_prompt(entry, prep)
    assert "x" * 500 not in prompt
    assert "y" * 500 not in prompt


# ---------- fire condition ----------

@pytest.mark.asyncio
async def test_fires_on_tomorrow_dated_entry(fresh_db, monkeypatch, gate_open, cadence_allow):
    tz = interview_brief._resolve_local_tz()
    today = datetime.now(tz).date()
    tomorrow_iso = (today + timedelta(days=1)).isoformat()

    monkeypatch.setattr(
        "tools.jobhunt.readers.interviews_upcoming",
        lambda _today: [_entry(when=tomorrow_iso)],
    )
    monkeypatch.setattr("tools.jobhunt.readers.prep_files", lambda slug: {})

    sent = []

    async def fake_send(text):
        sent.append(text)
        return (text, 1, True)

    async def fake_compose(prompt):
        return "no prep folder for acme yet. run /prep acme."

    monkeypatch.setattr(interview_brief, "run_visible_proactive", fake_compose)

    assert await interview_brief.maybe_send_interview_brief(fake_send) is True
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_does_not_fire_on_today_dated_entry(fresh_db, monkeypatch, gate_open, cadence_allow):
    tz = interview_brief._resolve_local_tz()
    today = datetime.now(tz).date()

    monkeypatch.setattr(
        "tools.jobhunt.readers.interviews_upcoming",
        lambda _today: [_entry(when=today.isoformat())],
    )
    monkeypatch.setattr("tools.jobhunt.readers.prep_files", lambda slug: {})

    async def fake_send(text):
        raise AssertionError("send_text must not be called")

    assert await interview_brief.maybe_send_interview_brief(fake_send) is False


@pytest.mark.asyncio
async def test_does_not_fire_on_next_week_dated_entry(fresh_db, monkeypatch, gate_open, cadence_allow):
    tz = interview_brief._resolve_local_tz()
    today = datetime.now(tz).date()
    next_week = (today + timedelta(days=7)).isoformat()

    monkeypatch.setattr(
        "tools.jobhunt.readers.interviews_upcoming",
        lambda _today: [_entry(when=next_week)],
    )
    monkeypatch.setattr("tools.jobhunt.readers.prep_files", lambda slug: {})

    async def fake_send(text):
        raise AssertionError("send_text must not be called")

    assert await interview_brief.maybe_send_interview_brief(fake_send) is False


@pytest.mark.asyncio
async def test_does_not_fire_on_none_dated_entry(fresh_db, monkeypatch, gate_open, cadence_allow):
    monkeypatch.setattr(
        "tools.jobhunt.readers.interviews_upcoming",
        lambda _today: [_entry(when=None, source="jobs", prep_state=None)],
    )
    monkeypatch.setattr("tools.jobhunt.readers.prep_files", lambda slug: {})

    async def fake_send(text):
        raise AssertionError("send_text must not be called")

    assert await interview_brief.maybe_send_interview_brief(fake_send) is False


@pytest.mark.asyncio
async def test_no_interviews_at_all_returns_false(fresh_db, monkeypatch, gate_open, cadence_allow):
    monkeypatch.setattr("tools.jobhunt.readers.interviews_upcoming", lambda _today: [])

    async def fake_send(text):
        raise AssertionError("send_text must not be called")

    assert await interview_brief.maybe_send_interview_brief(fake_send) is False


# ---------- dedup per slug+date ----------

@pytest.mark.asyncio
async def test_dedup_per_slug_and_date(fresh_db, monkeypatch, gate_open, cadence_allow):
    tz = interview_brief._resolve_local_tz()
    today = datetime.now(tz).date()
    tomorrow_iso = (today + timedelta(days=1)).isoformat()

    monkeypatch.setattr(
        "tools.jobhunt.readers.interviews_upcoming",
        lambda _today: [_entry(when=tomorrow_iso)],
    )
    monkeypatch.setattr("tools.jobhunt.readers.prep_files", lambda slug: {})

    sent = []

    async def fake_send(text):
        sent.append(text)
        return (text, 1, True)

    async def fake_compose(prompt):
        return "no prep folder for acme yet."

    monkeypatch.setattr(interview_brief, "run_visible_proactive", fake_compose)

    assert await interview_brief.maybe_send_interview_brief(fake_send) is True
    assert len(sent) == 1
    assert db.runtime_get(f"interview_brief_sent:acme:{tomorrow_iso}") == "1"

    # Second call for the SAME slug+date must not fire again.
    assert await interview_brief.maybe_send_interview_brief(fake_send) is False
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_dedup_is_per_slug_not_global(fresh_db, monkeypatch, gate_open, cadence_allow):
    """A dedup marker for one slug+date must not suppress a different slug on
    the same date."""
    tz = interview_brief._resolve_local_tz()
    today = datetime.now(tz).date()
    tomorrow_iso = (today + timedelta(days=1)).isoformat()

    db.runtime_set(f"interview_brief_sent:acme:{tomorrow_iso}", "1")

    monkeypatch.setattr(
        "tools.jobhunt.readers.interviews_upcoming",
        lambda _today: [_entry(org="Beta", slug="beta", when=tomorrow_iso)],
    )
    monkeypatch.setattr("tools.jobhunt.readers.prep_files", lambda slug: {})

    sent = []

    async def fake_send(text):
        sent.append(text)
        return (text, 1, True)

    async def fake_compose(prompt):
        return "no prep folder for beta yet."

    monkeypatch.setattr(interview_brief, "run_visible_proactive", fake_compose)

    assert await interview_brief.maybe_send_interview_brief(fake_send) is True
    assert len(sent) == 1


# ---------- multiple interviews on the same tomorrow ----------

@pytest.mark.asyncio
async def test_two_tomorrow_interviews_both_send(fresh_db, monkeypatch, gate_open, cadence_allow):
    """Two tomorrow-dated entries in one call: both send, both markers written."""
    tz = interview_brief._resolve_local_tz()
    today = datetime.now(tz).date()
    tomorrow_iso = (today + timedelta(days=1)).isoformat()

    monkeypatch.setattr(
        "tools.jobhunt.readers.interviews_upcoming",
        lambda _today: [
            _entry(org="Acme", slug="acme", when=tomorrow_iso),
            _entry(org="Beta", slug="beta", when=tomorrow_iso),
        ],
    )
    monkeypatch.setattr("tools.jobhunt.readers.prep_files", lambda slug: {})

    sent = []

    async def fake_send(text):
        sent.append(text)
        return (text, len(sent), True)

    async def fake_compose(prompt):
        return "no prep folder yet."

    monkeypatch.setattr(interview_brief, "run_visible_proactive", fake_compose)

    assert await interview_brief.maybe_send_interview_brief(fake_send) is True
    assert len(sent) == 2
    assert db.runtime_get(f"interview_brief_sent:acme:{tomorrow_iso}") == "1"
    assert db.runtime_get(f"interview_brief_sent:beta:{tomorrow_iso}") == "1"


@pytest.mark.asyncio
async def test_compose_failure_for_one_org_does_not_block_the_other(
    fresh_db, monkeypatch, gate_open, cadence_allow,
):
    """Org A's composition raising must not abort the loop: org B still sends,
    and only B's marker is written (A retains no marker → not falsely deduped)."""
    tz = interview_brief._resolve_local_tz()
    today = datetime.now(tz).date()
    tomorrow_iso = (today + timedelta(days=1)).isoformat()

    monkeypatch.setattr(
        "tools.jobhunt.readers.interviews_upcoming",
        lambda _today: [
            _entry(org="Acme", slug="acme", when=tomorrow_iso),
            _entry(org="Beta", slug="beta", when=tomorrow_iso),
        ],
    )
    monkeypatch.setattr("tools.jobhunt.readers.prep_files", lambda slug: {})

    async def flaky_compose(prompt):
        # entries are processed in order; acme's prompt names Acme
        if "Acme" in prompt:
            raise RuntimeError("SDK exploded")
        return "no prep folder for beta yet."

    monkeypatch.setattr(interview_brief, "run_visible_proactive", flaky_compose)

    sent = []

    async def fake_send(text):
        sent.append(text)
        return (text, 1, True)

    assert await interview_brief.maybe_send_interview_brief(fake_send) is True
    assert len(sent) == 1
    assert "beta" in sent[0]
    assert db.runtime_get(f"interview_brief_sent:acme:{tomorrow_iso}") is None
    assert db.runtime_get(f"interview_brief_sent:beta:{tomorrow_iso}") == "1"


# ---------- cadence veto / gate abort pass-throughs ----------

@pytest.mark.asyncio
async def test_cadence_veto_blocks_send(fresh_db, monkeypatch, gate_open):
    from agents import cadence
    tz = interview_brief._resolve_local_tz()
    today = datetime.now(tz).date()
    tomorrow_iso = (today + timedelta(days=1)).isoformat()

    monkeypatch.setattr(
        "tools.jobhunt.readers.interviews_upcoming",
        lambda _today: [_entry(when=tomorrow_iso)],
    )
    monkeypatch.setattr("tools.jobhunt.readers.prep_files", lambda slug: {})
    monkeypatch.setattr(cadence, "can_send", lambda source, pool=None: (False, "cap_reached"))

    async def fake_send(text):
        raise AssertionError("send_text must not be called when cadence vetoes")

    assert await interview_brief.maybe_send_interview_brief(fake_send) is False
    # No dedup marker written on a veto — nothing was actually sent.
    assert db.runtime_get(f"interview_brief_sent:acme:{tomorrow_iso}") is None


@pytest.mark.asyncio
async def test_gate_abort_blocks_marker_write(fresh_db, monkeypatch, cadence_allow):
    """When the proactive_gate aborts (e.g. quiet hours), no dedup marker is
    written, so the briefing can still fire on a later tick."""
    import agents.proactive_gate as _gate
    monkeypatch.setattr(_gate, "_is_quiet_now", lambda _db=None: True)

    tz = interview_brief._resolve_local_tz()
    today = datetime.now(tz).date()
    tomorrow_iso = (today + timedelta(days=1)).isoformat()

    monkeypatch.setattr(
        "tools.jobhunt.readers.interviews_upcoming",
        lambda _today: [_entry(when=tomorrow_iso)],
    )
    monkeypatch.setattr("tools.jobhunt.readers.prep_files", lambda slug: {})

    async def fake_compose(prompt):
        return "no prep folder for acme yet."
    monkeypatch.setattr(interview_brief, "run_visible_proactive", fake_compose)

    sent = []

    async def fake_send(text):
        sent.append(text)
        return (text, 1, True)

    assert await interview_brief.maybe_send_interview_brief(fake_send) is False
    assert sent == []
    assert db.runtime_get(f"interview_brief_sent:acme:{tomorrow_iso}") is None


@pytest.mark.asyncio
async def test_jobhunt_disabled_short_circuits(fresh_db, monkeypatch, gate_open, cadence_allow):
    from agents import config as cfg
    monkeypatch.setattr(
        cfg, "get",
        lambda key, default=None: False if key == "jobhunt.enabled" else default,
    )

    called = {"readers": False}

    def _boom(_today):
        called["readers"] = True
        return []
    monkeypatch.setattr("tools.jobhunt.readers.interviews_upcoming", _boom)

    async def fake_send(text):
        raise AssertionError("send_text must not be called")

    assert await interview_brief.maybe_send_interview_brief(fake_send) is False
    assert called["readers"] is False
