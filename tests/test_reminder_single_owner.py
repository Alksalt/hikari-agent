"""9C-1: Single-owner reminders — proactive.py is the sole firing path."""
from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml


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


@pytest.mark.asyncio
async def test_fire_due_reminders_marks_fired_in_same_transaction():
    """After fire_due_reminders sends, db.reminder_get(id).fired_at is set."""
    from agents import proactive
    from storage import db

    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    rid = db.reminder_insert(fire_at=past, text="test reminder", lead_minutes=0, repeat=None)

    sent: list[str] = []

    async def fake_send(text: str) -> tuple[str, int | None, bool]:
        sent.append(text)
        return text, 42, True

    fired = await proactive.fire_due_reminders(fake_send)

    assert fired == 1, f"expected 1 fired, got {fired}"
    row = db.reminder_get(rid)
    assert row is not None
    assert row["status"] == "fired", f"expected status=fired, got {row['status']!r}"
    assert row["fired_at"] is not None, "fired_at should be set after successful send"


def test_reminder_fire_producer_is_silent_awareness():
    """reminder_fire is enabled-but-silent: it may surface candidates for the
    selector's suppression window but must NEVER be a second firing path.
    Flipping send_mode to 'proactive' would resurrect the double-fire bug
    sprint-9 killed."""
    config_path = Path(__file__).parent.parent / "config" / "engagement.yaml"
    with open(config_path) as f:
        data = yaml.safe_load(f)

    reminder_fire = data.get("engagement", {}).get("reminder_fire")
    assert reminder_fire is not None, "reminder_fire block must exist"
    assert reminder_fire.get("enabled") is True
    assert reminder_fire.get("send_mode") == "silent", (
        f"reminder_fire send_mode must stay 'silent' — fire_due_reminders is "
        f"the sole firing path. got: {reminder_fire.get('send_mode')!r}"
    )
