"""Phase D: 3-pool cadence governor tests.

Covers:
  - Pool independence: counts are tracked separately per pool
  - allowed_sources routing: correct pool resolved from source
  - Cap reached within a pool does not block other pools
  - Governor disabled bypass
  - Unknown source returns source_not_justified
  - record_* helpers increment the correct pool counter
"""
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


def _fill_pool_log(pool_key: str, n: int) -> None:
    """Fill the given runtime_state key with n timestamps in the last 7d."""
    from storage import db
    now = datetime.now(UTC)
    log = [(now - timedelta(hours=i)).isoformat() for i in range(n)]
    db.runtime_set(pool_key, json.dumps(log))


# ---------- basic can_send ----------

def test_can_send_unknown_source_returns_source_not_justified():
    from agents.cadence import can_send
    allowed, reason = can_send("totally_unknown_source")
    assert allowed is False
    assert "source_not_justified" in reason


def test_can_send_with_explicit_pool_allows_known_source():
    from agents.cadence import Pool, can_send
    allowed, reason = can_send("open_loop", Pool.AGENT_SPONTANEOUS)
    assert allowed is True
    assert reason == "ok"


def test_can_send_resolves_pool_from_source():
    from agents.cadence import can_send
    # open_loop -> agent_spontaneous
    allowed, reason = can_send("open_loop")
    assert allowed is True
    assert reason == "ok"


def test_can_send_resolves_ceremony_source():
    from agents.cadence import can_send
    # daily_checkin -> scheduled_ceremony
    allowed, reason = can_send("daily_checkin")
    assert allowed is True
    assert reason == "ok"


def test_can_send_resolves_user_anchored_source():
    from agents.cadence import can_send
    # wiki_new_file -> user_anchored
    allowed, reason = can_send("wiki_new_file")
    assert allowed is True
    assert reason == "ok"


# ---------- cap enforcement ----------

def test_spontaneous_cap_blocks_when_reached():
    from agents.cadence import Pool, can_send
    # Fill to max (8)
    _fill_pool_log("proactive_log_v1", 8)
    allowed, reason = can_send("open_loop", Pool.AGENT_SPONTANEOUS)
    assert allowed is False
    assert "cap_reached" in reason
    assert "agent_spontaneous" in reason


def test_ceremony_cap_does_not_block_spontaneous():
    """Filling ceremony pool doesn't affect spontaneous pool."""
    from agents.cadence import Pool, can_send
    _fill_pool_log("proactive_ceremony_log_v1", 14)  # fill ceremony to max
    allowed, reason = can_send("open_loop", Pool.AGENT_SPONTANEOUS)
    assert allowed is True
    assert reason == "ok"


def test_spontaneous_cap_does_not_block_ceremony():
    """Filling spontaneous pool doesn't block ceremony pool."""
    from agents.cadence import Pool, can_send
    _fill_pool_log("proactive_log_v1", 8)  # fill spontaneous to max
    allowed, reason = can_send("daily_checkin", Pool.SCHEDULED_CEREMONY)
    assert allowed is True
    assert reason == "ok"


def test_user_anchored_has_its_own_cap():
    from agents.cadence import Pool, can_send
    _fill_pool_log("proactive_user_anchored_log_v1", 30)
    allowed, reason = can_send("wiki_new_file", Pool.USER_ANCHORED)
    assert allowed is False
    assert "cap_reached" in reason
    assert "user_anchored" in reason


# ---------- source-in-wrong-pool rejection ----------

def test_source_in_wrong_pool_is_rejected():
    """daily_checkin is ceremony — passing it to spontaneous pool rejects it."""
    from agents.cadence import Pool, can_send
    allowed, reason = can_send("daily_checkin", Pool.AGENT_SPONTANEOUS)
    assert allowed is False
    assert "source_not_in_pool" in reason


# ---------- governor disabled ----------

def test_governor_disabled_allows_any_source(monkeypatch):
    from agents import cadence
    monkeypatch.setattr(cadence, "_governor_enabled", lambda: False)
    _fill_pool_log("proactive_log_v1", 100)
    allowed, reason = cadence.can_send("open_loop")
    assert allowed is True
    assert reason == "governor_disabled"


# ---------- record_* helpers ----------

def test_record_spontaneous_sent_increments_spontaneous_pool():
    from agents import cadence
    from storage import db
    cadence.record_spontaneous_sent("open_loop")
    raw = db.runtime_get("proactive_log_v1")
    data = json.loads(raw)
    assert len(data) == 1


def test_record_ceremony_sent_increments_ceremony_pool():
    from agents import cadence
    from storage import db
    cadence.record_ceremony_sent("daily_checkin")
    raw = db.runtime_get("proactive_ceremony_log_v1")
    data = json.loads(raw)
    assert len(data) == 1


def test_record_user_anchored_sent_increments_user_anchored_pool():
    from agents import cadence
    from storage import db
    cadence.record_user_anchored_sent("wiki_new_file")
    raw = db.runtime_get("proactive_user_anchored_log_v1")
    data = json.loads(raw)
    assert len(data) == 1


def test_records_are_independent():
    """Incrementing one pool does not affect another."""
    from agents import cadence
    from storage import db
    cadence.record_spontaneous_sent("open_loop")
    cadence.record_spontaneous_sent("open_loop")
    cadence.record_ceremony_sent("daily_checkin")
    s_raw = json.loads(db.runtime_get("proactive_log_v1"))
    c_raw = json.loads(db.runtime_get("proactive_ceremony_log_v1"))
    u_raw = json.loads(db.runtime_get("proactive_user_anchored_log_v1") or "[]")
    assert len(s_raw) == 2
    assert len(c_raw) == 1
    assert len(u_raw) == 0


# ---------- compat shims ----------

def test_can_send_proactive_compat_shim_routes_spontaneous():
    from agents.cadence import can_send_proactive
    allowed, reason = can_send_proactive("open_loop")
    assert allowed is True


def test_can_send_proactive_compat_shim_routes_ceremony():
    from agents.cadence import can_send_proactive
    allowed, reason = can_send_proactive("daily_checkin")
    assert allowed is True


def test_can_send_proactive_compat_shim_unknown_source():
    from agents.cadence import can_send_proactive
    allowed, reason = can_send_proactive("totally_unknown")
    assert allowed is False
    assert "source_not_justified" in reason


def test_record_proactive_sent_compat_shim_increments_spontaneous():
    from agents import cadence
    from storage import db
    cadence.record_proactive_sent()
    raw = db.runtime_get("proactive_log_v1")
    data = json.loads(raw)
    assert len(data) == 1
