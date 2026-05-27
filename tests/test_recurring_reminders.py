"""Tests for recurrence.next_occurrence + fire_due_reminders rescheduling.

Covers:
  - All six cadence types: daily, weekly, monthly:N, monthly:last, yearly:MM-DD,
    every_n_days:N
  - End-of-month clamping (Jan 31 → Feb 28/29)
  - DST spring-forward preservation: same wall-clock time on the other side
  - Two-firing cycle: fire_due_reminders updates due_at between calls
"""
from __future__ import annotations

import importlib
import os
from datetime import UTC, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.reminders.recurrence import next_occurrence


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc(year: int, month: int, day: int, hour: int = 9, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, 0, tzinfo=UTC)


def _as_utc(dt: datetime) -> datetime:
    """Normalize any tz-aware datetime back to UTC for date/time assertions."""
    return dt.astimezone(UTC)


# ---------------------------------------------------------------------------
# Fixture: force UTC home tz so pure-unit tests are tz-agnostic
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _force_utc_home_tz(monkeypatch):
    """Pin HOME_TZ=UTC for all cadence unit tests so assertions on
    .day/.month/.hour don't shift with the host machine's locale.

    DST tests override HOME_TZ themselves and re-import recurrence — the
    monkeypatch teardown at the end of each test restores the env, so the
    final importlib.reload re-establishes UTC for the next test.
    """
    monkeypatch.setenv("HOME_TZ", "UTC")
    # Reload the module so _home_tz() picks up the patched env.
    from tools.reminders import recurrence as _rec_mod
    importlib.reload(_rec_mod)
    yield
    # Restore module state to UTC after each test (handles DST test overrides).
    importlib.reload(_rec_mod)


# ---------------------------------------------------------------------------
# Cadence unit tests (next_occurrence pure function)
# ---------------------------------------------------------------------------

class TestDaily:
    def test_same_wall_time_next_day(self):
        base = _utc(2026, 5, 15, 9, 0)
        result = _as_utc(next_occurrence("daily", base))
        assert result.day == 16
        assert result.month == 5
        assert result.hour == 9

    def test_crosses_month_boundary(self):
        base = _utc(2026, 5, 31, 9, 0)
        result = _as_utc(next_occurrence("daily", base))
        assert result.month == 6
        assert result.day == 1

    def test_crosses_year_boundary(self):
        base = _utc(2025, 12, 31, 9, 0)
        result = _as_utc(next_occurrence("daily", base))
        assert result.year == 2026
        assert result.month == 1
        assert result.day == 1


class TestEveryNDays:
    def test_14_days(self):
        base = _utc(2026, 5, 15, 9, 0)
        result = _as_utc(next_occurrence("every_n_days:14", base))
        expected = _as_utc(base.replace(day=29))
        assert result.day == expected.day
        assert result.month == expected.month

    def test_123_days(self):
        base = _utc(2026, 1, 1, 9, 0)
        result = _as_utc(next_occurrence("every_n_days:123", base))
        from datetime import timedelta
        expected = base + timedelta(days=123)
        assert result.year == expected.year
        assert result.month == expected.month
        assert result.day == expected.day

    def test_1_day_equivalent_to_daily(self):
        base = _utc(2026, 3, 10, 9, 0)
        r1 = _as_utc(next_occurrence("every_n_days:1", base))
        r2 = _as_utc(next_occurrence("daily", base))
        assert r1.day == r2.day
        assert r1.month == r2.month


class TestEveryNMinutes:
    def test_20_minutes(self):
        from datetime import timedelta
        base = _utc(2026, 5, 27, 19, 0)
        result = _as_utc(next_occurrence("every_n_minutes:20", base))
        assert result == base + timedelta(minutes=20)

    def test_1_minute_low_bound(self):
        from datetime import timedelta
        base = _utc(2026, 5, 27, 19, 0)
        result = _as_utc(next_occurrence("every_n_minutes:1", base))
        assert result == base + timedelta(minutes=1)

    def test_1440_minutes_high_bound(self):
        from datetime import timedelta
        base = _utc(2026, 5, 27, 19, 0)
        result = _as_utc(next_occurrence("every_n_minutes:1440", base))
        assert result == base + timedelta(minutes=1440)

    def test_zero_minutes_rejected(self):
        from tools.reminders.recurrence import validate_rule
        with pytest.raises(ValueError, match="every_n_minutes"):
            validate_rule("every_n_minutes:0")

    def test_above_1440_rejected(self):
        from tools.reminders.recurrence import validate_rule
        with pytest.raises(ValueError, match="every_n_minutes"):
            validate_rule("every_n_minutes:1441")

    def test_dst_spring_forward_preserves_minute_arithmetic(self, monkeypatch):
        """20-min cadence walks the absolute clock — DST shift is part of arithmetic.

        Sub-day cadences explicitly do not preserve wall-clock time (that's a
        daily/weekly concern). Spring-forward day, +20min before the gap should
        still be 20 absolute minutes later.
        """
        monkeypatch.setenv("HOME_TZ", "Europe/Oslo")
        from tools.reminders import recurrence
        importlib.reload(recurrence)
        from datetime import timedelta
        # 2027-03-28 02:00 Europe/Oslo doesn't exist (jumps to 03:00).
        # 01:50 Oslo + 20 min → 03:10 Oslo wall clock = +20 absolute minutes.
        oslo_tz = __import__("zoneinfo").ZoneInfo("Europe/Oslo")
        pre_dst = datetime(2027, 3, 28, 1, 50, 0, tzinfo=oslo_tz)
        result = recurrence.next_occurrence("every_n_minutes:20", pre_dst)
        # Absolute UTC delta is exactly 20 minutes.
        assert (result - pre_dst) == timedelta(minutes=20)


class TestEveryNHours:
    def test_2_hours(self):
        from datetime import timedelta
        base = _utc(2026, 5, 27, 19, 0)
        result = _as_utc(next_occurrence("every_n_hours:2", base))
        assert result == base + timedelta(hours=2)

    def test_1_hour_low_bound(self):
        from datetime import timedelta
        base = _utc(2026, 5, 27, 19, 0)
        result = _as_utc(next_occurrence("every_n_hours:1", base))
        assert result == base + timedelta(hours=1)

    def test_168_hours_high_bound(self):
        from datetime import timedelta
        base = _utc(2026, 5, 27, 19, 0)
        result = _as_utc(next_occurrence("every_n_hours:168", base))
        assert result == base + timedelta(hours=168)

    def test_zero_hours_rejected(self):
        from tools.reminders.recurrence import validate_rule
        with pytest.raises(ValueError, match="every_n_hours"):
            validate_rule("every_n_hours:0")

    def test_above_168_rejected(self):
        from tools.reminders.recurrence import validate_rule
        with pytest.raises(ValueError, match="every_n_hours"):
            validate_rule("every_n_hours:169")


class TestWeekly:
    def test_monday_fires_monday_next_is_wednesday(self):
        # 2026-05-25 is a Monday (isoweekday=1)
        monday = _utc(2026, 5, 25, 9, 0)
        assert monday.isoweekday() == 1
        result = _as_utc(next_occurrence("weekly:MON,WED,FRI", monday))
        # Next after Monday is Wednesday (isoweekday=3)
        assert result.isoweekday() == 3

    def test_wednesday_next_is_friday(self):
        # 2026-05-27 is a Wednesday
        wednesday = _utc(2026, 5, 27, 9, 0)
        assert wednesday.isoweekday() == 3
        result = _as_utc(next_occurrence("weekly:MON,WED,FRI", wednesday))
        assert result.isoweekday() == 5

    def test_friday_wraps_to_monday(self):
        friday = _utc(2026, 5, 29, 9, 0)
        assert friday.isoweekday() == 5
        result = _as_utc(next_occurrence("weekly:MON,WED,FRI", friday))
        assert result.isoweekday() == 1

    def test_saturday_wraps_to_monday(self):
        saturday = _utc(2026, 5, 30, 9, 0)
        assert saturday.isoweekday() == 6
        result = _as_utc(next_occurrence("weekly:MON,WED,FRI", saturday))
        assert result.isoweekday() == 1

    def test_wall_time_preserved(self):
        monday = datetime(2026, 5, 25, 14, 30, 0, tzinfo=UTC)
        result = _as_utc(next_occurrence("weekly:MON,WED,FRI", monday))
        # With HOME_TZ=UTC, wall-clock time is preserved exactly.
        assert result.hour == 14
        assert result.minute == 30


class TestMonthlyN:
    def test_jan_1_fires_jan_to_feb_1(self):
        jan_1 = _utc(2026, 1, 1, 9, 0)
        result = _as_utc(next_occurrence("monthly:1", jan_1))
        assert result.month == 2
        assert result.day == 1

    def test_jan_31_next_is_feb_28_non_leap(self):
        jan_31 = _utc(2026, 1, 31, 9, 0)
        result = _as_utc(next_occurrence("monthly:1", jan_31))
        # monthly:1 means "day 1 of next month", not "31 of next month"
        assert result.month == 2
        assert result.day == 1

    def test_jan_31_monthly_31_clamps_to_feb_28_non_leap(self):
        """monthly:31 from Jan-31 → Feb 28 (2026 is not a leap year)."""
        jan_31 = _utc(2026, 1, 31, 9, 0)
        result = _as_utc(next_occurrence("monthly:31", jan_31))
        assert result.month == 2
        assert result.day == 28

    def test_jan_31_monthly_31_clamps_to_feb_29_leap(self):
        """monthly:31 from Jan-31 → Feb 29 in a leap year (2028)."""
        jan_31 = _utc(2028, 1, 31, 9, 0)
        result = _as_utc(next_occurrence("monthly:31", jan_31))
        assert result.month == 2
        assert result.day == 29

    def test_crosses_year_boundary(self):
        dec_1 = _utc(2026, 12, 1, 9, 0)
        result = _as_utc(next_occurrence("monthly:1", dec_1))
        assert result.year == 2027
        assert result.month == 1
        assert result.day == 1


class TestMonthlyLast:
    def test_jan_31_next_is_feb_last_non_leap(self):
        jan_31 = _utc(2026, 1, 31, 10, 0)
        result = _as_utc(next_occurrence("monthly:last", jan_31))
        # Feb 2026 has 28 days
        assert result.month == 2
        assert result.day == 28

    def test_jan_31_next_is_feb_29_leap(self):
        jan_31 = _utc(2028, 1, 31, 10, 0)
        result = _as_utc(next_occurrence("monthly:last", jan_31))
        # Feb 2028 is a leap year → 29 days
        assert result.month == 2
        assert result.day == 29

    def test_dec_last_day_wraps_year(self):
        dec_31 = _utc(2026, 12, 31, 9, 0)
        result = _as_utc(next_occurrence("monthly:last", dec_31))
        assert result.year == 2027
        assert result.month == 1
        assert result.day == 31


class TestYearly:
    def test_dec_25_2026_next_is_dec_25_2027(self):
        dec_25 = _utc(2026, 12, 25, 9, 0)
        result = _as_utc(next_occurrence("yearly:12-25", dec_25))
        assert result.year == 2027
        assert result.month == 12
        assert result.day == 25

    def test_before_date_same_year(self):
        """If current date is before the yearly target, fire this year."""
        jan_1 = _utc(2026, 1, 1, 9, 0)
        result = _as_utc(next_occurrence("yearly:12-25", jan_1))
        assert result.year == 2026
        assert result.month == 12
        assert result.day == 25

    def test_wall_time_preserved(self):
        base = datetime(2026, 12, 25, 15, 45, 0, tzinfo=UTC)
        result = _as_utc(next_occurrence("yearly:12-25", base))
        assert result.year == 2027
        # With HOME_TZ=UTC, wall time is preserved exactly.
        assert result.hour == 15
        assert result.minute == 45


# ---------------------------------------------------------------------------
# DST transition test
# ---------------------------------------------------------------------------

class TestDstTransition:
    """Spring-forward 2027-03-28 in Europe/Oslo (UTC+1 → UTC+2, 02:00 jumps to 03:00).

    A reminder set for 01:30 local should land at 01:30 local the next day
    (or next applicable day), NOT at an incorrect UTC offset.
    """

    def test_daily_dst_spring_forward_preserves_wall_time(self, monkeypatch):
        monkeypatch.setenv("HOME_TZ", "Europe/Oslo")
        from tools.reminders import recurrence
        importlib.reload(recurrence)

        # Day before DST transition: 2027-03-27 09:00 Oslo (UTC+1 = 08:00 UTC)
        oslo_tz = __import__("zoneinfo").ZoneInfo("Europe/Oslo")
        pre_dst = datetime(2027, 3, 27, 9, 0, 0, tzinfo=oslo_tz)

        result = recurrence.next_occurrence("daily", pre_dst)
        local_result = result.astimezone(oslo_tz)

        # Wall-clock time should still be 09:00 Oslo (even though clocks sprang forward)
        assert local_result.hour == 9, (
            f"Expected 09:00 Oslo after DST, got {local_result.hour:02d}:{local_result.minute:02d}"
        )
        assert local_result.day == 28

    def test_weekly_dst_spring_forward_preserves_wall_time(self, monkeypatch):
        monkeypatch.setenv("HOME_TZ", "Europe/Oslo")
        from tools.reminders import recurrence
        importlib.reload(recurrence)

        oslo_tz = __import__("zoneinfo").ZoneInfo("Europe/Oslo")
        # 2027-03-27 is Saturday (isoweekday=6); weekly:SUN should fire tomorrow
        pre_dst = datetime(2027, 3, 27, 14, 0, 0, tzinfo=oslo_tz)

        result = recurrence.next_occurrence("weekly:SUN", pre_dst)
        local_result = result.astimezone(oslo_tz)

        assert local_result.isoweekday() == 7  # Sunday
        assert local_result.hour == 14


# ---------------------------------------------------------------------------
# Two-firing integration: fire_due_reminders updates due_at in DB
# ---------------------------------------------------------------------------

@pytest.fixture()
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from storage import db as _db
    monkeypatch.setattr(_db, "_DB_PATH", db_path)
    return _db


@pytest.mark.asyncio
async def test_fire_due_reminders_updates_due_at_across_two_firings(_isolated_db, monkeypatch):
    """After two consecutive fires, due_at must have advanced twice."""
    db = _isolated_db

    # Insert a reminder that is already past-due.
    past = datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC)
    rid = db.reminder_insert(
        fire_at=past.isoformat(),
        text="test recurring",
        recurrence_rule="daily",
    )

    dispatched: list[str] = []

    async def _fake_send(text: str):
        dispatched.append(text)
        return (text, 42, True)

    # Patch reserve_and_send to simulate a successful send.
    import agents.proactive as _proactive
    mock_reserve = AsyncMock()
    mock_reserve.return_value = MagicMock(status="sent", reason=None)
    monkeypatch.setattr(_proactive, "reserve_and_send", mock_reserve)

    # First fire.
    from agents.proactive import fire_due_reminders
    count = await fire_due_reminders(_fake_send)
    assert count == 1

    row_after_first = db.reminder_list(active_only=True)
    assert any(r["id"] == rid for r in row_after_first), "Row should still be active"
    first_due = next(r["fire_at"] for r in row_after_first if r["id"] == rid)
    assert first_due != past.isoformat(), "due_at must have been advanced after first fire"

    # Manually set due_at back to past so it fires again.
    db.reminder_update_fire_at(rid, past.isoformat())

    # Second fire.
    count2 = await fire_due_reminders(_fake_send)
    assert count2 == 1

    row_after_second = db.reminder_list(active_only=True)
    second_due = next(r["fire_at"] for r in row_after_second if r["id"] == rid)
    assert second_due != past.isoformat(), "due_at must have been advanced after second fire"
    # Both fires advance from the same past timestamp, so first_due == second_due
    # is expected — what matters is that the update happened both times.
    assert second_due == first_due, (
        "Both fires advance from the same anchor, so the new due_at should be identical"
    )


@pytest.mark.asyncio
async def test_fire_due_reminders_does_not_mark_recurrence_reminder_fired(_isolated_db, monkeypatch):
    """Recurrence reminders must remain 'active' after firing (infinite loop)."""
    db = _isolated_db

    past = datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC)
    rid = db.reminder_insert(
        fire_at=past.isoformat(),
        text="recurring test",
        recurrence_rule="every_n_days:7",
    )

    async def _fake_send(text: str):
        return (text, 42, True)

    import agents.proactive as _proactive
    mock_reserve = AsyncMock()
    mock_reserve.return_value = MagicMock(status="sent", reason=None)
    monkeypatch.setattr(_proactive, "reserve_and_send", mock_reserve)

    from agents.proactive import fire_due_reminders
    await fire_due_reminders(_fake_send)

    rows = db.reminder_list(active_only=True)
    assert any(r["id"] == rid for r in rows), (
        "Recurring reminder must stay active after firing"
    )
