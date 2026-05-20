"""Evening diary feature tests.

Mirrors the isolation pattern in ``tests/test_daily_checkin_composer.py``
(``HIKARI_DB_PATH`` env + ``_reset_schema_sentinel``) and the day_receipt
isolation in ``tests/test_day_receipt.py`` (``DAY_RECEIPT_DB`` env +
the day_receipt schema sentinel reset). Both have to be reset because
the diary pulls signals from both stores.
"""
from __future__ import annotations

import importlib
from datetime import date as _date
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    """Isolate both the hikari.db and day_receipt SQLite files per test.

    Mirrors ``tests/test_daily_checkin_composer.py``: ``importlib.reload``
    forces ``storage.db`` to re-read its module-level ``_DB_PATH`` constant
    from the monkeypatched env. The day_receipt module reads its env on
    every ``db_path()`` call, so only the schema sentinel needs resetting
    on that side.
    """
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


# ---------- helpers ----------

def _today_iso() -> str:
    from agents.evening_diary import _today_local_iso
    return _today_local_iso()


# ---------- gather ----------

@pytest.mark.asyncio
async def test_gather_day_data_collects_receipts_and_reminders():
    from agents import evening_diary
    from storage import db
    from tools.day_receipt._db import add_entry

    today_iso = _today_iso()
    today = _date.fromisoformat(today_iso)

    add_entry("made", "shipped the prototype", today, tags=("work",))
    db.reminder_insert(fire_at=f"{today_iso} 12:00:00", text="ping mom")
    # Mark it fired today so the gather sees it.
    with db._conn() as conn:
        conn.execute(
            "UPDATE reminders SET status='fired', fired_at=? WHERE 1=1",
            (f"{today_iso} 12:00:05",),
        )
    db.insert_episode(today_iso, "today was a long one. mostly worked.", 5)

    data = await evening_diary.gather_day_data(today_iso)

    assert data["date"] == today_iso
    assert "shipped the prototype" in data["receipts"]["made"]
    assert len(data["reminders_fired"]) == 1
    assert data["reminders_fired"][0]["text"] == "ping mom"
    assert len(data["episodes_today"]) == 1
    assert "long one" in data["episodes_today"][0]["summary"]


@pytest.mark.asyncio
async def test_gather_day_data_empty_day_returns_empty_lists():
    from agents import evening_diary

    today_iso = _today_iso()
    data = await evening_diary.gather_day_data(today_iso)

    assert data["date"] == today_iso
    assert isinstance(data["receipts"], dict)
    for cat in ("made", "moved", "learned", "avoided"):
        assert data["receipts"].get(cat) == []
    assert data["reminders_fired"] == []
    assert data["episodes_today"] == []
    assert data["note"] is None


# ---------- prompt ----------

def test_build_prompt_embeds_all_categories():
    from agents import evening_diary

    data = {
        "date": "2026-05-20",
        "receipts": {
            "made": ["shipped the prototype"],
            "moved": ["walked 8k"],
            "learned": ["read about attention mechanisms"],
            "avoided": ["didn't doomscroll"],
        },
        "reminders_fired": [{"text": "ping mom", "fired_at": "2026-05-20 12:00:00"}],
        "episodes_today": [{"summary": "today was fine", "importance": 5}],
        "note": "the rice was cold and good",
    }
    prompt = evening_diary.build_prompt(data)

    # Every category surfaces with its count + a verbatim entry.
    assert "made (1)" in prompt
    assert "moved (1)" in prompt
    assert "learned (1)" in prompt
    assert "avoided (1)" in prompt
    assert "shipped the prototype" in prompt
    assert "walked 8k" in prompt
    assert "attention mechanisms" in prompt
    assert "didn't doomscroll" in prompt
    # Fired reminders surface by text.
    assert "ping mom" in prompt
    # Note surfaces.
    assert "the rice was cold and good" in prompt
    # Episodes surface as a one-line summary.
    assert "episodes logged today" in prompt
    # Voice rules + sentinel.
    assert "NO_ENTRY" in prompt
    assert "lowercase" in prompt
    assert "4-8 sentence" in prompt


# ---------- compose ----------

@pytest.mark.asyncio
async def test_compose_diary_calls_run_internal_control(monkeypatch):
    from agents import evening_diary

    expected = "today i finally fixed the migration. tired. mou. that's it."
    mock = AsyncMock(return_value=expected)
    monkeypatch.setattr(evening_diary, "run_internal_control", mock)

    data = {
        "date": "2026-05-20",
        "receipts": {"made": ["x"], "moved": [], "learned": [], "avoided": []},
        "reminders_fired": [],
        "episodes_today": [],
        "note": None,
    }
    text = await evening_diary.compose_diary(data)

    assert text == expected
    # And the prompt that went to the SDK contained the data.
    sent_prompt = mock.call_args[0][0]
    assert "2026-05-20" in sent_prompt
    assert "made (1)" in sent_prompt


@pytest.mark.asyncio
async def test_compose_diary_rejects_no_entry_sentinel(monkeypatch):
    from agents import evening_diary

    monkeypatch.setattr(
        evening_diary, "run_internal_control",
        AsyncMock(return_value="NO_ENTRY"),
    )
    data = {
        "date": "2026-05-20",
        "receipts": {c: [] for c in ("made", "moved", "learned", "avoided")},
        "reminders_fired": [], "episodes_today": [], "note": None,
    }
    assert await evening_diary.compose_diary(data) is None


@pytest.mark.asyncio
async def test_compose_diary_rejects_sdk_error_string(monkeypatch):
    from agents import evening_diary

    monkeypatch.setattr(
        evening_diary, "run_internal_control",
        AsyncMock(return_value="Failed to authenticate. API Error: 401 socket closed"),
    )
    data = {
        "date": "2026-05-20",
        "receipts": {c: [] for c in ("made", "moved", "learned", "avoided")},
        "reminders_fired": [], "episodes_today": [], "note": None,
    }
    assert await evening_diary.compose_diary(data) is None


# ---------- write ----------

def test_write_diary_file_creates_path(tmp_path: Path):
    from agents import evening_diary

    path = evening_diary.write_diary_file(
        "2026-05-20", "today was fine.\n", root=tmp_path,
    )
    assert path.exists()
    assert path == tmp_path / "data" / "diary" / "2026-05-20.md"
    assert "today was fine" in path.read_text(encoding="utf-8")


def test_write_diary_file_idempotent(tmp_path: Path):
    from agents import evening_diary

    p1 = evening_diary.write_diary_file(
        "2026-05-20", "first version.", root=tmp_path,
    )
    body_after_first = p1.read_text(encoding="utf-8")
    # Second call with different body must NOT overwrite.
    p2 = evening_diary.write_diary_file(
        "2026-05-20", "second version (should not land).", root=tmp_path,
    )
    assert p1 == p2
    assert p2.read_text(encoding="utf-8") == body_after_first


# ---------- orchestrator ----------

@pytest.mark.asyncio
async def test_run_evening_diary_full_flow_writes_file_and_episode(
    tmp_path: Path, monkeypatch,
):
    from agents import evening_diary
    from storage import db

    expected = "fixed the migration. shower. bed. mou."
    monkeypatch.setattr(
        evening_diary, "run_internal_control",
        AsyncMock(return_value=expected),
    )

    today_iso = _today_iso()
    ok = await evening_diary.run_evening_diary(today=today_iso, root=tmp_path)
    assert ok is True

    diary_file = tmp_path / "data" / "diary" / f"{today_iso}.md"
    assert diary_file.exists()
    assert expected in diary_file.read_text(encoding="utf-8")

    # And the episode landed in the DB.
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT summary FROM episodes WHERE date = ?", (today_iso,),
        ).fetchall()
    summaries = [r["summary"] for r in rows]
    assert expected in summaries


@pytest.mark.asyncio
async def test_run_evening_diary_skips_if_file_exists(tmp_path: Path, monkeypatch):
    from agents import evening_diary

    today_iso = _today_iso()
    diary_dir = tmp_path / "data" / "diary"
    diary_dir.mkdir(parents=True)
    (diary_dir / f"{today_iso}.md").write_text(
        "already written this morning.\n", encoding="utf-8",
    )

    mock = AsyncMock(return_value="should never be called")
    monkeypatch.setattr(evening_diary, "run_internal_control", mock)

    ok = await evening_diary.run_evening_diary(today=today_iso, root=tmp_path)
    assert ok is False
    assert mock.await_count == 0
