"""Regression guard for Fix 1 (Sprint 2 review): ceremony proactive_events rows
must have telegram_message_id populated (not None) after a successful send.

Without the fix, _safe_send / inline send_text capture dropped the tg_id from
the (text, tg_id, ok) tuple and always wrote telegram_message_id=None, which
meant reaction-handler WHERE telegram_message_id=? queries found zero rows and
thumbs_up/thumbs_down could never be updated.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    monkeypatch.setenv("HOME_TZ", "Europe/Berlin")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from agents import config
    config.reload()
    yield


@pytest.fixture(autouse=True)
def _gate_open(monkeypatch):
    """Keep the proactive gate open — quiet-hours / silence checks are tested
    in test_proactive_global_reservation.py, not here."""
    import agents.proactive_gate as _gate
    monkeypatch.setattr(_gate, "_is_quiet_now", lambda _db=None: False)
    monkeypatch.setattr(_gate, "_silence_active", lambda _db: False)


# ---------------------------------------------------------------------------
# daily_checkin
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_daily_checkin_proactive_event_has_tg_id(monkeypatch):
    """maybe_run_daily_checkin must write the Telegram message_id into
    proactive_events so reaction joins work."""
    from agents import daily_checkin, cadence

    fake_tg_id = 7777

    # Force should_fire_now → True.
    monkeypatch.setattr(daily_checkin, "should_fire_now", lambda _: True)
    # Force cadence governor to allow.
    monkeypatch.setattr(cadence, "can_send", lambda source, pool=None: (True, "ok"))
    monkeypatch.setattr(cadence, "record_ceremony_sent", lambda source: None)
    # Stub composer.
    monkeypatch.setattr(daily_checkin, "compose_checkin_question",
                        AsyncMock(return_value="morning. emails?"))
    # Stub mark_fired_today / clear_expired_overrides so no HOME_TZ dep.
    monkeypatch.setattr(daily_checkin, "mark_fired_today", lambda _: None)
    monkeypatch.setattr(daily_checkin, "clear_expired_overrides", lambda _: None)

    async def fake_send(text: str):
        return (text, fake_tg_id, True)

    result = await daily_checkin.maybe_run_daily_checkin(fake_send)
    assert result is True

    with db._conn() as c:
        rows = c.execute(
            "SELECT source, telegram_message_id FROM proactive_events "
            "WHERE source='daily_checkin'"
        ).fetchall()

    assert len(rows) == 1, f"expected 1 proactive_events row, got {len(rows)}"
    assert rows[0]["telegram_message_id"] == fake_tg_id, (
        f"expected telegram_message_id={fake_tg_id}, "
        f"got {rows[0]['telegram_message_id']!r} — "
        "reaction joins will never match without this"
    )


# ---------------------------------------------------------------------------
# decision_log
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_decision_log_proactive_event_has_tg_id(monkeypatch):
    """run_decision_resolver must write the Telegram message_id into
    proactive_events so reaction joins work."""
    from agents import decision_log, cadence

    fake_tg_id = 8888

    # Seed one overdue decision using the correct db function.
    db.decision_insert("ship by friday", 0.8, "2020-01-01")

    monkeypatch.setattr(cadence, "can_send", lambda source, pool=None: (True, "ok"))
    monkeypatch.setattr(cadence, "record_ceremony_sent", lambda source: None)

    async def fake_send(text: str):
        return (text, fake_tg_id, True)

    asked = await decision_log.run_decision_resolver(fake_send)
    assert asked == 1

    with db._conn() as c:
        rows = c.execute(
            "SELECT source, telegram_message_id FROM proactive_events "
            "WHERE source='decision_log'"
        ).fetchall()

    assert len(rows) == 1, f"expected 1 proactive_events row, got {len(rows)}"
    assert rows[0]["telegram_message_id"] == fake_tg_id, (
        f"expected telegram_message_id={fake_tg_id}, "
        f"got {rows[0]['telegram_message_id']!r} — "
        "reaction joins will never match without this"
    )


# ---------------------------------------------------------------------------
# future_letter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_future_letter_proactive_event_has_tg_id(monkeypatch):
    """run_future_letter must write the Telegram message_id from the last
    chunk into proactive_events so reaction joins work."""
    from agents import future_letter, cadence

    fake_tg_id = 9999
    fake_body = "dear you, it's been a while."

    monkeypatch.setattr(cadence, "can_send", lambda source, pool=None: (True, "ok"))
    monkeypatch.setattr(cadence, "record_ceremony_sent", lambda source: None)

    # Stub the entire pipeline above the send loop.
    monkeypatch.setattr(future_letter, "gather_month_data",
                        AsyncMock(return_value={"receipts": ["a"] * 10, "episodes": ["b"]}))
    monkeypatch.setattr(future_letter, "_has_enough_data", lambda data, min_r: True)
    monkeypatch.setattr(future_letter, "pick_decision_theme",
                        AsyncMock(return_value="growth"))
    monkeypatch.setattr(future_letter, "compose_letter",
                        AsyncMock(return_value=fake_body))
    monkeypatch.setattr(future_letter, "write_letter_file", lambda *a, **kw: None)

    async def fake_send(text: str):
        return (text, fake_tg_id, True)

    result = await future_letter.run_future_letter(fake_send)
    assert result is True

    with db._conn() as c:
        rows = c.execute(
            "SELECT source, telegram_message_id FROM proactive_events "
            "WHERE source='future_letter_send'"
        ).fetchall()

    assert len(rows) == 1, f"expected 1 proactive_events row, got {len(rows)}"
    assert rows[0]["telegram_message_id"] == fake_tg_id, (
        f"expected telegram_message_id={fake_tg_id}, "
        f"got {rows[0]['telegram_message_id']!r} — "
        "reaction joins will never match without this"
    )
