"""9C-2: decision_resolve_due dedup — proactive_gate marks asked after successful send."""
from __future__ import annotations

import importlib
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
    from agents import config
    config.reload()


@pytest.fixture(autouse=True)
def _gate_open(monkeypatch):
    import agents.proactive_gate as _gate
    monkeypatch.setattr(_gate, "_is_quiet_now", lambda _db=None: False)
    monkeypatch.setattr(_gate, "_silence_active", lambda _db: False)


def _insert_due_decision() -> int:
    from storage import db
    return db.decision_insert(
        statement="will ship by friday?",
        predicted_p=0.7,
        resolve_by="2025-01-01",  # past date — overdue
    )


@pytest.mark.asyncio
async def test_decision_marked_asked_after_successful_send():
    """After reserve_and_send with ok=True and decision_resolve_due dedup_key,
    the decision row has asked_at set."""
    from storage import db
    from agents.proactive_gate import reserve_and_send

    did = _insert_due_decision()

    async def ok_send(text: str) -> tuple[str, int | None, bool]:
        return text, 99, True

    result = await reserve_and_send(
        send_text_fn=ok_send,
        producer_id="decision_resolve_due",
        pattern="question",
        text="did 'will ship by friday?' happen?",
        dedup_key=f"decision_resolve_due:{did}",
        db=db,
    )

    assert result.status == "sent"
    row = db.decisions_unresolved_due(limit=10, cooldown_days=0)
    # With cooldown_days=0 the row still appears; check asked_at directly.
    with db._conn() as c:
        decision = c.execute("SELECT asked_at FROM decisions WHERE id = ?", (did,)).fetchone()
    assert decision["asked_at"] is not None, "asked_at should be set after successful send"


@pytest.mark.asyncio
async def test_decision_not_marked_asked_on_failed_send():
    """When send fails (ok=False), asked_at must remain None."""
    from storage import db
    from agents.proactive_gate import reserve_and_send

    did = _insert_due_decision()

    async def fail_send(text: str) -> tuple[str, int | None, bool]:
        return text, None, False

    result = await reserve_and_send(
        send_text_fn=fail_send,
        producer_id="decision_resolve_due",
        pattern="question",
        text="did 'will ship by friday?' happen?",
        dedup_key=f"decision_resolve_due:{did}",
        db=db,
    )

    assert result.status == "aborted"
    with db._conn() as c:
        decision = c.execute("SELECT asked_at FROM decisions WHERE id = ?", (did,)).fetchone()
    assert decision["asked_at"] is None, "asked_at must not be set when send fails"


def test_re_asking_within_cooldown_suppressed():
    """After decision_mark_asked, decisions_unresolved_due(cooldown_days=14) excludes the row."""
    from storage import db

    did = _insert_due_decision()
    rows_before = db.decisions_unresolved_due(limit=10, cooldown_days=14)
    assert any(r["id"] == did for r in rows_before), "decision should appear before mark_asked"

    db.decision_mark_asked(did)

    rows_after = db.decisions_unresolved_due(limit=10, cooldown_days=14)
    assert not any(r["id"] == did for r in rows_after), (
        "decision should be suppressed within cooldown after mark_asked"
    )
