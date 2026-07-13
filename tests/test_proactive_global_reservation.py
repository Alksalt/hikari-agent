"""Sprint 4 Phase 4B — proactive global reservation gate."""
import asyncio
import importlib
import json
from datetime import UTC, datetime, timedelta

import pytest


@pytest.fixture
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    import storage.db as db_mod
    importlib.reload(db_mod)
    db_mod._reset_schema_sentinel()
    # Trigger schema init by doing a read (lazy-init via _get_pooled_conn).
    db_mod.get_session_id()
    yield db_mod


async def _ok_send(text):
    return (text, 42, True)


async def _fail_send(text):
    return (text, None, False)


@pytest.mark.asyncio
async def test_lock_serializes_concurrent_producers(_isolated_db, monkeypatch):
    """Two concurrent reserve_and_send calls must NOT overlap inside send_text_fn."""
    from agents import proactive_gate
    # Reset the module-level lock so it isn't shared with prior tests.
    importlib.reload(proactive_gate)
    monkeypatch.setattr(proactive_gate, "_is_quiet_now", lambda _db=None: False)

    enters = []

    async def slow_send(text):
        enters.append(("enter", asyncio.get_event_loop().time()))
        await asyncio.sleep(0.05)
        enters.append(("exit", asyncio.get_event_loop().time()))
        return (text, 1, True)

    t1 = proactive_gate.reserve_and_send(
        send_text_fn=slow_send, producer_id="p1", pattern="x", text="one",
        db=_isolated_db,
    )
    t2 = proactive_gate.reserve_and_send(
        send_text_fn=slow_send, producer_id="p2", pattern="x", text="two",
        db=_isolated_db,
    )
    await asyncio.gather(t1, t2)

    # 4 events: enter/exit/enter/exit, never enter,enter,exit,exit
    kinds = [e[0] for e in enters]
    assert kinds == ["enter", "exit", "enter", "exit"], kinds


@pytest.mark.asyncio
async def test_silence_window_aborts(_isolated_db, monkeypatch):
    import importlib

    from agents import proactive_gate
    importlib.reload(proactive_gate)
    monkeypatch.setattr(proactive_gate, "_is_quiet_now", lambda _db=None: False)
    until = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    _isolated_db.runtime_set("silence_until", until)

    called = []

    async def fake(text):
        called.append(text)
        return (text, 1, True)

    result = await proactive_gate.reserve_and_send(
        send_text_fn=fake, producer_id="p", pattern="x", text="hi",
        db=_isolated_db,
    )
    assert result.status == "aborted"
    assert result.reason == "silence_window"
    assert called == []
    # row in DB
    with _isolated_db._conn() as c:
        row = c.execute("SELECT status, aborted_reason FROM proactive_events").fetchone()
    assert row["status"] == "aborted"
    assert row["aborted_reason"] == "silence_window"


@pytest.mark.asyncio
async def test_quiet_hours_aborts(_isolated_db, monkeypatch):
    import importlib

    from agents import proactive_gate
    importlib.reload(proactive_gate)
    monkeypatch.setattr(proactive_gate, "_is_quiet_now", lambda _db=None: True)
    result = await proactive_gate.reserve_and_send(
        send_text_fn=_ok_send, producer_id="p", pattern="x", text="hi",
        db=_isolated_db,
    )
    assert result.status == "aborted"
    assert result.reason == "quiet_hours"


@pytest.mark.asyncio
async def test_send_failure_aborts(_isolated_db, monkeypatch):
    import importlib

    from agents import proactive_gate
    importlib.reload(proactive_gate)
    monkeypatch.setattr(proactive_gate, "_is_quiet_now", lambda _db=None: False)
    result = await proactive_gate.reserve_and_send(
        send_text_fn=_fail_send, producer_id="p", pattern="x", text="hi",
        db=_isolated_db,
    )
    assert result.status == "aborted"
    assert result.reason == "send_failed"
    with _isolated_db._conn() as c:
        row = c.execute("SELECT status, aborted_reason FROM proactive_events").fetchone()
    assert row["status"] == "aborted"
    assert row["aborted_reason"] == "send_failed"


@pytest.mark.asyncio
async def test_dedup_hit_aborts_second(_isolated_db, monkeypatch):
    import importlib

    from agents import proactive_gate
    importlib.reload(proactive_gate)
    monkeypatch.setattr(proactive_gate, "_is_quiet_now", lambda _db=None: False)

    payload = json.dumps({"reminder_id": 7})
    r1 = await proactive_gate.reserve_and_send(
        send_text_fn=_ok_send, producer_id="reminder", pattern="fire",
        text="t1", payload_json=payload, dedup_key="reminder:7",
        db=_isolated_db,
    )
    assert r1.status == "sent"

    r2 = await proactive_gate.reserve_and_send(
        send_text_fn=_ok_send, producer_id="reminder", pattern="fire",
        text="t2", payload_json=payload, dedup_key="reminder:7",
        db=_isolated_db,
    )
    assert r2.status == "aborted"
    assert r2.reason == "dedup"


@pytest.mark.asyncio
async def test_durable_dedup_survives_event_retention(_isolated_db, monkeypatch):
    """Exact mail-action receipts outlive the prunable engagement audit row."""
    import importlib

    from agents import proactive_gate
    importlib.reload(proactive_gate)
    monkeypatch.setattr(proactive_gate, "_is_quiet_now", lambda _db=None: False)
    sends = []

    async def send(text):
        sends.append(text)
        return (text, 808, True)

    first = await proactive_gate.reserve_and_send(
        send_text_fn=send,
        producer_id="mail_decisions",
        pattern="urgent_mail_action",
        text="interview",
        dedup_key="mail_decisions:808",
        durable_dedup=True,
        db=_isolated_db,
    )
    assert first.status == "sent"

    old = (datetime.now(UTC) - timedelta(days=120)).isoformat()
    with _isolated_db._conn() as c:
        c.execute("UPDATE proactive_events SET sent_at = ? WHERE id = ?", (old, first.event_id))
    assert _isolated_db.prune_proactive_events(older_than_days=90) == 1
    assert _isolated_db.proactive_delivery_receipt_exists(
        "mail_decisions", "mail_decisions:808"
    )

    second = await proactive_gate.reserve_and_send(
        send_text_fn=send,
        producer_id="mail_decisions",
        pattern="urgent_mail_action",
        text="interview again",
        dedup_key="mail_decisions:808",
        durable_dedup=True,
        db=_isolated_db,
    )
    assert second.reason == "dedup"
    assert sends == ["interview"]


@pytest.mark.asyncio
async def test_empty_text_short_circuits(_isolated_db, monkeypatch):
    import importlib

    from agents import proactive_gate
    importlib.reload(proactive_gate)
    result = await proactive_gate.reserve_and_send(
        send_text_fn=_ok_send, producer_id="p", pattern="x", text="",
        db=_isolated_db,
    )
    assert result.status == "aborted"
    assert result.reason == "empty_text"
    # no row written
    with _isolated_db._conn() as c:
        n = c.execute("SELECT COUNT(*) FROM proactive_events").fetchone()[0]
    assert n == 0
