"""Intent parser: maps a short user reply to ``{email: bool, calendar: bool}``.

Schedule-edit parser: detects natural-language commands like
'check in at 06:30 tomorrow' and returns a ScheduleEdit dict to apply."""
from __future__ import annotations

import importlib
from datetime import datetime
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    monkeypatch.setenv("HOME_TZ", "Europe/Berlin")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from agents import config
    config.reload()
    yield


@pytest.mark.parametrize("text,expected", [
    # Both yes
    ("yes both", {"email": True, "calendar": True}),
    ("both", {"email": True, "calendar": True}),
    ("yes", {"email": True, "calendar": True}),
    ("yeah", {"email": True, "calendar": True}),
    ("ok", {"email": True, "calendar": True}),
    ("go", {"email": True, "calendar": True}),
    ("do it", {"email": True, "calendar": True}),
    # Both no
    ("no", {"email": False, "calendar": False}),
    ("nope", {"email": False, "calendar": False}),
    ("skip", {"email": False, "calendar": False}),
    ("not now", {"email": False, "calendar": False}),
    # Selective
    ("just email", {"email": True, "calendar": False}),
    ("only inbox", {"email": True, "calendar": False}),
    ("just calendar", {"email": False, "calendar": True}),
    ("only cal", {"email": False, "calendar": True}),
    ("emails only", {"email": True, "calendar": False}),
])
def test_parse_intent_table(text, expected):
    from agents.daily_checkin import parse_intent
    assert parse_intent(text) == expected


@pytest.mark.parametrize("text", [
    "i'm not sure",
    "what?",
    "tell me about the weather",
    "",
])
def test_parse_intent_ambiguous_returns_none(text):
    from agents.daily_checkin import parse_intent
    assert parse_intent(text) is None


def test_parse_schedule_edit_one_shot_override():
    from agents.daily_checkin import parse_schedule_edit
    today = datetime(2026, 5, 21).date()
    edit = parse_schedule_edit("check in at 06:30 tomorrow", today=today)
    assert edit == {"kind": "override", "date": "2026-05-22", "time": "06:30"}


def test_parse_schedule_edit_default_change():
    from agents.daily_checkin import parse_schedule_edit
    edit = parse_schedule_edit("from now on check in at 08:00", today=datetime(2026, 5, 21).date())
    assert edit == {"kind": "default", "time": "08:00"}
    edit2 = parse_schedule_edit("set morning check to 06:45", today=datetime(2026, 5, 21).date())
    assert edit2 == {"kind": "default", "time": "06:45"}


def test_parse_schedule_edit_skip():
    from agents.daily_checkin import parse_schedule_edit
    today = datetime(2026, 5, 21).date()
    edit = parse_schedule_edit("skip the morning check tomorrow", today=today)
    assert edit == {"kind": "skip", "date": "2026-05-22"}


def test_parse_schedule_edit_query():
    from agents.daily_checkin import parse_schedule_edit
    today = datetime(2026, 5, 21).date()
    edit = parse_schedule_edit("what time is my check-in?", today=today)
    assert edit == {"kind": "query"}


def test_parse_schedule_edit_no_match():
    from agents.daily_checkin import parse_schedule_edit
    today = datetime(2026, 5, 21).date()
    assert parse_schedule_edit("hey, what's up", today=today) is None
    assert parse_schedule_edit("yes", today=today) is None


def test_apply_schedule_edit_override():
    from agents.daily_checkin import _load_schedule, apply_schedule_edit
    apply_schedule_edit({"kind": "override", "date": "2026-05-22", "time": "06:30"})
    s = _load_schedule()
    assert s.get("override_date") == "2026-05-22"
    assert s.get("override_time") == "06:30"


def test_apply_schedule_edit_default():
    from agents.daily_checkin import _load_schedule, apply_schedule_edit
    apply_schedule_edit({"kind": "default", "time": "08:00"})
    assert _load_schedule().get("default_time") == "08:00"


def test_apply_schedule_edit_skip_appends():
    from agents.daily_checkin import _load_schedule, apply_schedule_edit
    apply_schedule_edit({"kind": "skip", "date": "2026-05-22"})
    apply_schedule_edit({"kind": "skip", "date": "2026-05-23"})
    s = _load_schedule()
    assert "2026-05-22" in s.get("skip_dates", [])
    assert "2026-05-23" in s.get("skip_dates", [])
