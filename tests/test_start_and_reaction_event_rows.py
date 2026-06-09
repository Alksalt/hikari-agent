"""Phase 13.1 (Stream K) — regression: event rows are compact, not instruction text.

Pins H-2 (reaction event row):

H-2: handle_message_reaction writes a compact "[reacted <emoji> to msg #<id>]"
     event row with source='event'.
     The long synthetic instruction text must NOT appear as a messages row.

(H-1, the /start event row, was removed in Phase 5b — cmd_start is gone;
"/start" now routes to handle_message as a normal conversational turn.)
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents import config
from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    config.reload()
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_reaction_update(emoji: str, message_id: int,
                          user_id: int = 12345, chat_id: int = 12345):
    rxn = SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(id=chat_id),
        message_id=message_id,
        new_reaction=[SimpleNamespace(emoji=emoji)],
    )
    return SimpleNamespace(message_reaction=rxn)


def _ctx_with_bot():
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=999)),
        send_chat_action=AsyncMock(),
    )
    return SimpleNamespace(bot=bot)


def _all_user_rows() -> list[dict]:
    with db._conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT content, source FROM messages WHERE role='user'"
        ).fetchall()]


# ---------------------------------------------------------------------------
# H-2: reaction event row
# ---------------------------------------------------------------------------

def _seed_prev_assistant(text: str, telegram_message_id: int) -> None:
    db.append_message("assistant", text)
    db.update_last_assistant_telegram_msg_id(telegram_message_id)


@pytest.mark.asyncio
async def test_reaction_turn_writes_compact_event_row(monkeypatch):
    """handle_message_reaction must write a compact '[reacted <emoji> to msg #<id>]' row."""
    from agents import telegram_bridge

    monkeypatch.setattr(telegram_bridge, "ReactionTypeEmoji", SimpleNamespace)

    async def fake_run_user_turn(prompt):
        return "ugh. fine."
    monkeypatch.setattr(telegram_bridge, "run_user_turn", fake_run_user_turn)

    sends = []
    async def fake_send_text_choreo(bot, chat_id, text, *, elapsed_real=0.0):
        sends.append(text)
    monkeypatch.setattr(telegram_bridge, "_send_text_with_choreography", fake_send_text_choreo)

    _seed_prev_assistant("hikari said something here", 600)
    update = _make_reaction_update("🌙", 600)

    await telegram_bridge.handle_message_reaction(update, _ctx_with_bot())

    rows = _all_user_rows()
    event_rows = [r for r in rows if r["source"] == "event"]
    assert len(event_rows) >= 1, (
        f"Expected at least one event row from reaction turn, got: {event_rows}"
    )
    # The reaction event row must match the compact format
    reaction_rows = [
        r for r in event_rows
        if re.match(r"\[reacted .+ to msg #\d+\]", r["content"])
    ]
    assert len(reaction_rows) == 1, (
        f"Expected exactly one compact reaction event row, got: {event_rows}"
    )


@pytest.mark.asyncio
async def test_reaction_turn_does_not_write_instruction_text_as_row(monkeypatch):
    """The synthetic instruction text must NOT appear as a user messages row."""
    from agents import telegram_bridge

    monkeypatch.setattr(telegram_bridge, "ReactionTypeEmoji", SimpleNamespace)

    async def fake_run_user_turn(prompt):
        return "hm."
    monkeypatch.setattr(telegram_bridge, "run_user_turn", fake_run_user_turn)

    async def fake_send_text_choreo(bot, chat_id, text, *, elapsed_real=0.0):
        pass
    monkeypatch.setattr(telegram_bridge, "_send_text_with_choreography", fake_send_text_choreo)

    _seed_prev_assistant("some previous message", 700)
    update = _make_reaction_update("👀", 700)

    await telegram_bridge.handle_message_reaction(update, _ctx_with_bot())

    rows = _all_user_rows()
    # The synthetic prompt ("the user reacted to your previous message with...")
    # must not appear as a user row
    bad_rows = [
        r for r in rows
        if "the user reacted to your previous message with" in r["content"]
        or "the user reacted with" in r["content"]
    ]
    assert bad_rows == [], (
        f"Synthetic instruction text leaked into messages: {bad_rows}"
    )
