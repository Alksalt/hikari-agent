"""Ghost-of-Future-Self letter routine tests.

Mirrors the isolation pattern in test_evening_diary.py — both day_receipt
and hikari.db are isolated per test because the letter pulls from both.
"""
from __future__ import annotations

import importlib
from datetime import date as _date
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    monkeypatch.setenv("DAY_RECEIPT_DB", str(tmp_path / "receipt.db"))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    monkeypatch.setenv("HOME_TZ", "Europe/Berlin")

    import storage.db as _db_mod
    importlib.reload(_db_mod)

    from tools.day_receipt import _db as _receipt_db
    _receipt_db._reset_schema_sentinel()

    yield tmp_path

    _receipt_db._reset_schema_sentinel()


# ---------- db helpers + schema ----------

def test_future_letter_table_created_and_unique_per_month():
    """The brand-new table + UNIQUE month_iso index should be in _SCHEMA."""
    from storage import db
    db.upsert_core_block("ping", "ping")  # forces schema bootstrap
    aid = db.future_letter_insert("2026-05", "the decision to ship", "body 1")
    assert aid > 0
    # Same month twice must violate the UNIQUE constraint.
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        db.future_letter_insert("2026-05", "another theme", "body 2")


def test_future_letter_get_and_mark_sent_round_trip():
    from storage import db
    db.future_letter_insert("2026-04", "the choice to slow down", "body")
    row = db.future_letter_get("2026-04")
    assert row is not None
    assert row["theme"] == "the choice to slow down"
    assert row["sent_at"] is None

    db.future_letter_mark_sent("2026-04")
    row = db.future_letter_get("2026-04")
    assert row["sent_at"] is not None


# ---------- chunking ----------

def test_chunk_for_telegram_single_when_under_limit():
    from agents.future_letter import _chunk_for_telegram
    body = "short letter"
    assert _chunk_for_telegram(body, 100) == ["short letter"]


def test_chunk_for_telegram_splits_on_paragraph_boundary():
    from agents.future_letter import _chunk_for_telegram
    body = "p one is here.\n\np two is longer and should land in chunk two."
    chunks = _chunk_for_telegram(body, 25)
    assert len(chunks) >= 2
    # First chunk should end at the paragraph break, not mid-word.
    assert chunks[0] == "p one is here."


# ---------- evidence formatting ----------

def test_format_evidence_block_includes_all_sections():
    from agents.future_letter import _format_evidence_block
    data = {
        "window_start": "2026-04-21", "window_end": "2026-05-21",
        "receipts": {
            "made": [{"date": "2026-05-10", "text": "shipped the api"}],
            "moved": [],
            "learned": [{"date": "2026-05-15", "text": "duckdb is faster"}],
            "avoided": [],
        },
        "episodes": [{"date": "2026-05-12",
                      "summary": "long meeting day. ate lunch late."}],
        "character_thoughts": [{"created_at": "2026-05-08T12:00:00",
                                "thought": "they were quieter today."}],
        "open_tasks": [{"subject": "draft Q3 plan", "importance": 7,
                        "created_at": "2026-04-30", "last_mention_at": "2026-05-18"}],
        "weekly_consolidations": [{"week_ending": "2026-05-17",
                                   "summary_text": "they shipped a thing.",
                                   "episode_count": 8}],
    }
    block = _format_evidence_block(data, per_cat_cap=5)
    assert "WINDOW: 2026-04-21 → 2026-05-21" in block
    assert "shipped the api" in block
    assert "duckdb is faster" in block
    assert "long meeting day" in block
    assert "they were quieter today" in block
    assert "draft Q3 plan" in block
    assert "they shipped a thing" in block


# ---------- has_enough_data gate ----------

def test_has_enough_data_with_receipts_only():
    from agents.future_letter import _has_enough_data
    data = {
        "receipts": {"made": [{"date": "x", "text": "a"}] * 5,
                     "moved": [], "learned": [], "avoided": []},
        "episodes": [],
    }
    assert _has_enough_data(data, min_receipts=5) is True
    assert _has_enough_data(data, min_receipts=6) is False


def test_has_enough_data_with_episodes_only():
    from agents.future_letter import _has_enough_data
    data = {
        "receipts": {"made": [], "moved": [], "learned": [], "avoided": []},
        "episodes": [{"date": "x", "summary": "y", "importance": 5}],
    }
    # Episodes alone are enough — even one is signal.
    assert _has_enough_data(data, min_receipts=5) is True


def test_has_enough_data_sparse():
    from agents.future_letter import _has_enough_data
    data = {
        "receipts": {"made": [], "moved": [], "learned": [], "avoided": []},
        "episodes": [],
    }
    assert _has_enough_data(data, min_receipts=5) is False


# ---------- build_composition_prompt ----------

def test_composition_prompt_contains_voice_rules_and_evidence():
    from agents.future_letter import build_composition_prompt
    data = {
        "window_start": "2026-04-21", "window_end": "2026-05-21",
        "receipts": {"made": [{"date": "2026-05-10", "text": "shipped X"}],
                     "moved": [], "learned": [], "avoided": []},
        "episodes": [],
        "character_thoughts": [], "open_tasks": [],
        "weekly_consolidations": [],
    }
    prompt = build_composition_prompt(
        data, "the decision to focus on the api", user_age=26,
    )
    # Honesty constraints — must all be present.
    assert "Cite at least 4" in prompt
    assert "didn't go as planned" in prompt
    assert "NO_LETTER" in prompt
    # Voice frame — must address the user 5 years in the future.
    assert "age 31" in prompt
    assert "age 26" in prompt
    # The theme is woven in.
    assert "the decision to focus on the api" in prompt
    # Concrete evidence carried through.
    assert "shipped X" in prompt
    # Opener instruction matches future-year math (2026 + 5 = 2031).
    assert "hey. it's 2031" in prompt


# ---------- gather_month_data (integration with both DBs) ----------

@pytest.mark.asyncio
async def test_gather_month_data_pulls_from_all_stores():
    from agents.future_letter import gather_month_data
    from storage import db
    from tools.day_receipt._db import add_entry

    today = _date.today()
    inside_window = today - timedelta(days=10)
    outside_window = today - timedelta(days=45)

    # Inside window — should appear.
    add_entry("made", "in-window receipt", inside_window)
    add_entry("avoided", "in-window avoided", inside_window)
    # Outside window — should NOT appear.
    add_entry("made", "stale receipt", outside_window)

    db.insert_episode(inside_window.isoformat(), "in-window episode", 5)
    db.insert_episode(outside_window.isoformat(), "stale episode", 5)

    # character_thought inside the window — uses ISO datetime ordering.
    db.append_thought("in-window thought")

    db.create_task("draft retro doc", importance=8)

    data = await gather_month_data(today.strftime("%Y-%m"))

    receipt_texts = [r["text"] for cat in data["receipts"].values() for r in cat]
    assert "in-window receipt" in receipt_texts
    assert "in-window avoided" in receipt_texts
    assert "stale receipt" not in receipt_texts

    ep_summaries = [e["summary"] for e in data["episodes"]]
    assert "in-window episode" in ep_summaries
    assert "stale episode" not in ep_summaries

    thoughts = [t["thought"] for t in data["character_thoughts"]]
    assert "in-window thought" in thoughts

    task_subjects = [t["subject"] for t in data["open_tasks"]]
    assert "draft retro doc" in task_subjects


# ---------- orchestrator end-to-end ----------

@pytest.mark.asyncio
async def test_run_future_letter_happy_path(monkeypatch, tmp_path: Path):
    """Compose + persist + send round-trip with mocked LLM and send_text."""
    from agents import future_letter
    from storage import db
    from tools.day_receipt._db import add_entry

    today = _date.today()
    for i in range(6):  # cross the min_receipts threshold
        add_entry("made", f"shipped thing {i}", today - timedelta(days=i))

    # Mock the LLM passes.
    async def fake_theme(prompt, *, max_turns, max_budget_usd):  # noqa: ARG001
        return "the decision to ship the api"

    async def fake_compose(prompt, *, max_turns, max_budget_usd):  # noqa: ARG001
        # 600-char body — under the 3800-char chunk limit so should be a
        # single chunk.
        return "hey. it's 2031.\n\n" + ("real letter body. " * 25)

    # The orchestrator calls run_internal_control twice. Distinguish them by
    # call order via a simple counter.
    call_count = {"n": 0}

    async def fake_run_internal_control(prompt, *, max_turns, max_budget_usd,
                                        extra_allowed_tools=None):  # noqa: ARG001
        call_count["n"] += 1
        if call_count["n"] == 1:
            return await fake_theme(prompt, max_turns=max_turns,
                                    max_budget_usd=max_budget_usd)
        return await fake_compose(prompt, max_turns=max_turns,
                                  max_budget_usd=max_budget_usd)

    import agents.runtime as runtime_mod
    monkeypatch.setattr(runtime_mod, "run_internal_control",
                        fake_run_internal_control)
    # The orchestrator imports the symbol at module load time, so patch the
    # alias in the future_letter module too.
    monkeypatch.setattr(future_letter, "run_internal_control",
                        fake_run_internal_control)

    sent_messages: list[str] = []

    async def fake_send(text):
        sent_messages.append(text)
        return text, 42, True

    month_iso = today.strftime("%Y-%m")
    ok = await future_letter.run_future_letter(
        fake_send, today=month_iso, root=tmp_path,
    )
    assert ok is True
    # Theme call + composition call.
    assert call_count["n"] == 2
    # Preamble + 1 chunk = 2 sends.
    assert len(sent_messages) == 2
    assert "i made you something" in sent_messages[0]
    assert "real letter body" in sent_messages[1]

    # Persisted in DB.
    row = db.future_letter_get(month_iso)
    assert row is not None
    assert row["theme"] == "the decision to ship the api"
    assert row["sent_at"] is not None

    # Persisted to disk.
    file_dir = tmp_path / "data" / "future_letters"
    target = file_dir / f"{month_iso}.md"
    assert target.exists()
    body_on_disk = target.read_text(encoding="utf-8")
    assert "real letter body" in body_on_disk
    assert "the decision to ship the api" in body_on_disk

    # Dedup marker set.
    assert db.runtime_get("future_letter_last_month") == month_iso


@pytest.mark.asyncio
async def test_run_future_letter_dedup_via_runtime_state(monkeypatch, tmp_path: Path):
    """Second run for the same month with the marker set should skip
    immediately — no LLM calls, no send."""
    from agents import future_letter
    from storage import db

    today_str = _date.today().strftime("%Y-%m")
    db.runtime_set("future_letter_last_month", today_str)

    llm_called = {"n": 0}

    async def fake_run_internal_control(*a, **k):  # noqa: ARG001
        llm_called["n"] += 1
        return "should not be called"

    monkeypatch.setattr(future_letter, "run_internal_control",
                        fake_run_internal_control)
    send_fake = AsyncMock(return_value=("x", 1, True))

    ok = await future_letter.run_future_letter(
        send_fake, today=today_str, root=tmp_path,
    )
    assert ok is False
    assert llm_called["n"] == 0
    send_fake.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_future_letter_sparse_data_skips(monkeypatch, tmp_path: Path):
    """If receipts < threshold and zero episodes, skip the LLM entirely."""
    from agents import future_letter

    llm_called = {"n": 0}

    async def fake_run_internal_control(*a, **k):  # noqa: ARG001
        llm_called["n"] += 1
        return "should not be called"

    monkeypatch.setattr(future_letter, "run_internal_control",
                        fake_run_internal_control)
    send_fake = AsyncMock(return_value=("x", 1, True))

    ok = await future_letter.run_future_letter(
        send_fake, today=_date.today().strftime("%Y-%m"), root=tmp_path,
    )
    assert ok is False
    assert llm_called["n"] == 0


@pytest.mark.asyncio
async def test_run_future_letter_composer_no_letter_skips_send(
    monkeypatch, tmp_path: Path,
):
    """If the composer returns NO_LETTER, no row, no file, no send."""
    from agents import future_letter
    from storage import db
    from tools.day_receipt._db import add_entry

    today = _date.today()
    for i in range(6):
        add_entry("made", f"thing {i}", today - timedelta(days=i))

    call_count = {"n": 0}

    async def fake_run_internal_control(prompt, *, max_turns,  # noqa: ARG001
                                        max_budget_usd,
                                        extra_allowed_tools=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return "the choice to pause"
        return "NO_LETTER"

    monkeypatch.setattr(future_letter, "run_internal_control",
                        fake_run_internal_control)
    send_fake = AsyncMock(return_value=("x", 1, True))

    month_iso = today.strftime("%Y-%m")
    ok = await future_letter.run_future_letter(
        send_fake, today=month_iso, root=tmp_path,
    )
    assert ok is False
    assert db.future_letter_get(month_iso) is None
    send_fake.assert_not_awaited()
