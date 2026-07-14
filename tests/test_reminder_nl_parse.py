"""NL fallback parsing for user-supplied reminder times (_parse_when).

ISO stays the instructed primary path (the model computes it from the
``# now`` block); ``_parse_when`` is the safety net for the rare case the
model passes a relative phrase verbatim. ``snooze`` deliberately stays on
strict ``_parse_iso`` — it parses DB-owned ``fire_at`` values where fuzzy
parsing a corrupt row would mask a bug.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from tools.reminders._shared import _parse_iso, _parse_when


def test_iso_still_parses_strict_and_first(monkeypatch):
    """A valid ISO string short-circuits — dateparser is never imported."""
    import builtins

    real_import = builtins.__import__

    def _no_dateparser(name, *a, **kw):
        if name == "dateparser":
            raise AssertionError("dateparser must not be imported for ISO input")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _no_dateparser)
    got = _parse_when("2026-05-20T18:05:00+00:00")
    assert got == datetime(2026, 5, 20, 18, 5, tzinfo=UTC)


def test_iso_naive_defaults_utc():
    got = _parse_when("2026-05-20T18:05:00")
    assert got == datetime(2026, 5, 20, 18, 5, tzinfo=UTC)


def test_nl_relative_en(monkeypatch):
    monkeypatch.setenv("HOME_TZ", "Europe/Kyiv")
    got = _parse_when("in 1 hour")
    assert got is not None
    expect = datetime.now(UTC) + timedelta(hours=1)
    assert abs((got - expect).total_seconds()) < 120


def test_nl_uk_tomorrow_morning(monkeypatch):
    monkeypatch.setenv("HOME_TZ", "Europe/Kyiv")
    got = _parse_when("завтра о 9")
    assert got is not None
    local = got.astimezone(ZoneInfo("Europe/Kyiv"))
    tomorrow = (datetime.now(ZoneInfo("Europe/Kyiv")) + timedelta(days=1)).date()
    assert local.date() == tomorrow
    assert local.hour == 9


def test_nl_ru_tomorrow_morning(monkeypatch):
    monkeypatch.setenv("HOME_TZ", "Europe/Kyiv")
    got = _parse_when("завтра в 9")
    assert got is not None
    local = got.astimezone(ZoneInfo("Europe/Kyiv"))
    tomorrow = (datetime.now(ZoneInfo("Europe/Kyiv")) + timedelta(days=1)).date()
    assert local.date() == tomorrow
    assert local.hour == 9


def test_tz_changes_utc_result(monkeypatch):
    """Each HOME_TZ resolves ``tomorrow`` against its own local calendar.

    Near midnight, Kyiv and UTC can already be on different dates.  The old
    assertion assumed they always shared a calendar day and became a -21h
    flake in that window.  Validate the exact UTC relationship from the two
    parsed local dates and Kyiv's offset instead.
    """
    monkeypatch.setenv("HOME_TZ", "Europe/Kyiv")
    kyiv = _parse_when("завтра о 9")
    monkeypatch.setenv("HOME_TZ", "UTC")
    utc = _parse_when("завтра о 9")
    assert kyiv is not None and utc is not None
    kyiv_local = kyiv.astimezone(ZoneInfo("Europe/Kyiv"))
    utc_local = utc.astimezone(UTC)
    assert kyiv_local.hour == utc_local.hour == 9
    date_delta_h = (utc_local.date() - kyiv_local.date()).days * 24
    kyiv_offset_h = kyiv_local.utcoffset().total_seconds() / 3600
    delta_h = (utc - kyiv).total_seconds() / 3600
    assert delta_h == date_delta_h + kyiv_offset_h


def test_result_is_utc(monkeypatch):
    monkeypatch.setenv("HOME_TZ", "Europe/Kyiv")
    got = _parse_when("in 30 minutes")
    assert got is not None
    assert got.tzinfo is not None
    assert got.utcoffset() == timedelta(0)


def test_garbage_returns_none():
    assert _parse_when("blah not a time") is None
    assert _parse_when("") is None
    assert _parse_when("   ") is None


def test_parse_iso_stays_strict():
    """The strict parser must NOT grow NL behavior (snooze depends on it)."""
    assert _parse_iso("in 1 hour") is None
    assert _parse_iso("завтра о 9") is None
