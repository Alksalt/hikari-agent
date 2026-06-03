"""send_and_persist: a transient Telegram send error must NOT permanently drop
the message on the first failure (2026-06-03 fix).

Before the fix, the inline failure path called media_outbox_mark_failed() with no
max_attempts, so a 'text' row flipped straight to 'failed' on the first
ConnectError — one network blip lost the message forever. With max_attempts=3 the
row stays retryable ('sending'), so the stale-sending reaper requeues it and the
drain re-sends.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield
    db._reset_schema_sentinel()


class _TransientError(Exception):
    """Stand-in for httpx.ConnectError / telegram.error.NetworkError."""


class _FailingBot:
    """A bot whose send_message always raises a transient network error."""

    async def send_message(self, chat_id: int, text: str):
        raise _TransientError("connection reset")

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        pass


def _outbox_rows():
    with db._conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM media_outbox ORDER BY id").fetchall()]


@pytest.mark.asyncio
async def test_transient_send_failure_keeps_row_retryable():
    from agents import messaging

    result = await messaging.send_and_persist(
        bot=_FailingBot(),
        chat_id=12345,
        text="don't lose me on one blip.",
        source="chat",
        already_filtered=True,
        run_hooks=False,
        skip_choreography=True,
    )

    # The inline send failed and the caller is told so.
    assert result.ok is False

    rows = _outbox_rows()
    assert len(rows) == 1, "expected exactly one outbox row for the attempted send"
    row = rows[0]
    # The regression: status must NOT be terminalized to 'failed' on attempt 1.
    assert row["status"] != "failed", (
        f"row terminalized on first transient failure (status={row['status']!r}) — "
        "the message would be permanently dropped"
    )
    assert row["attempts"] == 1
