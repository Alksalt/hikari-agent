"""Cadence governor: daily_checkin source must bypass the 7d cap."""
from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from agents import config
    config.reload()
    yield


def _fill_proactive_log(n: int) -> None:
    """Fill the SPONTANEOUS pool log (proactive_log_v1) with n timestamps."""
    from storage import db
    now = datetime.now(UTC)
    log = [(now - timedelta(hours=i)).isoformat() for i in range(n)]
    db.runtime_set("proactive_log_v1", json.dumps(log))


def _fill_ceremony_log(n: int) -> None:
    """Fill the CEREMONY pool log with n timestamps."""
    from storage import db
    now = datetime.now(UTC)
    log = [(now - timedelta(hours=i)).isoformat() for i in range(n)]
    db.runtime_set("proactive_ceremony_log_v1", json.dumps(log))


def test_cap_blocks_normal_source_at_max():
    from agents import cadence
    _fill_proactive_log(8)
    allowed, reason = cadence.can_send_proactive("open_loop")
    assert allowed is False
    assert "cap_reached" in reason


def test_daily_checkin_source_uses_its_own_pool_and_bypasses_spontaneous_cap():
    """daily_checkin is in the ceremony pool (cap=14). Filling the spontaneous
    pool (cap=8) should NOT block daily_checkin — they are independent."""
    from agents import cadence
    _fill_proactive_log(10)  # fill spontaneous pool past its cap
    allowed, reason = cadence.can_send_proactive("daily_checkin")
    assert allowed is True
    assert reason == "ok"


def test_daily_checkin_passes_when_governor_disabled(monkeypatch):
    """If the governor is fully disabled, every source path returns ok —
    including daily_checkin."""
    from agents import cadence
    monkeypatch.setattr(cadence, "_governor_enabled", lambda: False)
    _fill_proactive_log(99)  # would saturate the cap if governor were on
    allowed, reason = cadence.can_send_proactive("daily_checkin")
    assert allowed is True
    assert reason == "governor_disabled"


def test_daily_checkin_blocked_when_ceremony_pool_full():
    """daily_checkin IS blocked when the ceremony pool (cap=14) is full."""
    from agents import cadence
    _fill_ceremony_log(14)
    allowed, reason = cadence.can_send_proactive("daily_checkin")
    assert allowed is False
    assert "cap_reached" in reason
