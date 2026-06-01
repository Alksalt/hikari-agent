"""belief-frame kwarg: internal_belief_context must NOT appear in persisted messages.

respond() persists RAW user_text only; the belief-frame augmentation is passed
exclusively to the SDK and must never reach the messages table.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import patch

import pytest

from storage import db

# ---------------------------------------------------------------------------
# DB isolation
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    yield


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_belief_context_does_not_appear_in_db():
    """internal_belief_context is forwarded to the SDK prompt but not persisted."""
    belief_suffix = "[BELIEF-FRAME: adversarial context injected here]"
    user_text = "I think 60% chance we ship friday"

    captured_sdk_prompt: list[str] = []

    async def _fake_run_user_turn(prompt: str) -> str:
        captured_sdk_prompt.append(prompt)
        return "logged."

    with patch("agents.runtime.run_user_turn", side_effect=_fake_run_user_turn):
        from agents.runtime import respond
        await respond(user_text, internal_belief_context=belief_suffix)

    # Only the user row should be written (no assistant row from the mock).
    with db._conn() as c:
        rows = c.execute("SELECT role, content FROM messages").fetchall()

    user_rows = [r for r in rows if r["role"] == "user"]
    assert len(user_rows) == 1
    assert user_rows[0]["content"] == user_text
    assert belief_suffix not in user_rows[0]["content"]

    # The SDK did receive the augmented prompt.
    assert len(captured_sdk_prompt) == 1
    assert belief_suffix in captured_sdk_prompt[0]
    assert user_text in captured_sdk_prompt[0]


@pytest.mark.asyncio
async def test_no_belief_context_sends_raw():
    """Without internal_belief_context, SDK prompt equals user_text verbatim."""
    user_text = "plain message"
    captured: list[str] = []

    async def _fake_run_user_turn(prompt: str) -> str:
        captured.append(prompt)
        return "ok"

    with patch("agents.runtime.run_user_turn", side_effect=_fake_run_user_turn):
        from agents.runtime import respond
        await respond(user_text)

    assert captured == [user_text]

    with db._conn() as c:
        rows = c.execute("SELECT content FROM messages WHERE role='user'").fetchall()
    assert len(rows) == 1
    assert rows[0]["content"] == user_text
