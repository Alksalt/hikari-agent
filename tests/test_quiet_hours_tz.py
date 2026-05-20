"""Phase 13.1 (Stream K) — regression: quiet hours use configured TZ.

Pins I-1 fix: _is_quiet_now() reads HOME_TZ / scheduler.timezone,
not the system-local timezone. This matters when the bot runs on a server
in a different timezone than the user.

Tests:
  - Monkeypatch agents.proactive._resolve_local_tz_name to return "Asia/Tokyo".
  - Freeze time to 02:00 UTC = 11:00 Tokyo.
  - quiet_start_hour=23, quiet_end_hour=8 → NOT quiet at 11:00 Tokyo.
  - quiet_start_hour=10, quiet_end_hour=14 → IS quiet at 11:00 Tokyo.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agents import config


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    config.reload()
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_is_quiet_now(quiet_start: int, quiet_end: int, utc_now: datetime,
                       monkeypatch):
    """Return the result of _is_quiet_now() with patched TZ + frozen time.

    Patches:
      - agents.proactive._resolve_local_tz_name → returns "Asia/Tokyo"
      - agents.proactive.datetime.now → returns utc_now.astimezone(tz)

    proactive._is_quiet_now reads _p() for quiet_start_hour / quiet_end_hour.
    We override _p via monkeypatch.
    """
    import agents.proactive as pmod
    from datetime import datetime as real_datetime

    # _resolve_local_tz_name is imported into proactive from hooks; patch
    # the name in the proactive module namespace directly.
    monkeypatch.setattr(pmod, "_resolve_local_tz_name", lambda: "Asia/Tokyo")

    # Override the proactive config dict
    monkeypatch.setattr(pmod, "_p", lambda: {
        "quiet_start_hour": quiet_start,
        "quiet_end_hour": quiet_end,
    })

    # Freeze datetime.now inside the proactive module.
    # _is_quiet_now uses: now = datetime.now(tz).time()
    # where datetime is the module-level name bound by
    # `from datetime import datetime`.
    class _FrozenDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            if tz is not None:
                return utc_now.astimezone(tz)
            return utc_now

    monkeypatch.setattr(pmod, "datetime", _FrozenDatetime)

    return pmod._is_quiet_now()


# 02:00 UTC = 11:00 JST (UTC+9)
_UTC_02 = datetime(2026, 5, 20, 2, 0, 0, tzinfo=UTC)


def test_not_quiet_at_11_tokyo_with_night_window(monkeypatch):
    """11:00 Tokyo is outside the night quiet window (23:00-08:00) → not quiet."""
    result = _make_is_quiet_now(
        quiet_start=23, quiet_end=8, utc_now=_UTC_02, monkeypatch=monkeypatch,
    )
    assert result is False, (
        "11:00 Tokyo should NOT be quiet when quiet_start=23, quiet_end=8 "
        "(night window). Got True — likely _resolve_local_tz_name is not "
        "reading the configured TZ."
    )


def test_quiet_at_11_tokyo_with_midday_window(monkeypatch):
    """11:00 Tokyo IS inside 10:00-14:00 quiet window → quiet."""
    result = _make_is_quiet_now(
        quiet_start=10, quiet_end=14, utc_now=_UTC_02, monkeypatch=monkeypatch,
    )
    assert result is True, (
        "11:00 Tokyo SHOULD be quiet when quiet_start=10, quiet_end=14. "
        "Got False — likely _resolve_local_tz_name is not reading the configured TZ."
    )


def test_not_quiet_at_11_tokyo_with_late_morning_window(monkeypatch):
    """11:00 Tokyo is outside 12:00-15:00 window → not quiet."""
    result = _make_is_quiet_now(
        quiet_start=12, quiet_end=15, utc_now=_UTC_02, monkeypatch=monkeypatch,
    )
    assert result is False, (
        "11:00 Tokyo should NOT be quiet when quiet_start=12, quiet_end=15. Got True."
    )


def test_resolve_local_tz_from_env(monkeypatch):
    """_resolve_local_tz_name returns HOME_TZ env when set."""
    monkeypatch.setenv("HOME_TZ", "Asia/Tokyo")
    # Reload module-level env read
    from agents.hooks import _resolve_local_tz_name
    result = _resolve_local_tz_name()
    assert result == "Asia/Tokyo", (
        f"Expected HOME_TZ='Asia/Tokyo', got {result!r}"
    )


def test_resolve_local_tz_from_config(monkeypatch, tmp_path):
    """_resolve_local_tz_name returns scheduler.timezone from config when HOME_TZ not set."""
    cfg_text = "scheduler:\n  timezone: 'America/New_York'\n"
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    monkeypatch.delenv("HOME_TZ", raising=False)
    config.reload()

    from agents.hooks import _resolve_local_tz_name
    result = _resolve_local_tz_name()
    assert result == "America/New_York", (
        f"Expected 'America/New_York' from config, got {result!r}"
    )
