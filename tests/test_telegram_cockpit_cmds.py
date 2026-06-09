"""Phase 5b (useful-agent pivot) — zero slash-commands regression tests.

The Telegram bridge registers NO CommandHandler instances. Operator control
moved to conversational tools (set_silence, set_proactive_source,
checkin_control, reminder_list, diary_read, link_search, receipt_read, ...)
and inline keyboards. Command-shaped texts ("/start") must fall through to
handle_message and become a normal conversational turn.

Cases:
  1. build_application registers zero CommandHandler instances (all groups).
  2. The handle_message MessageHandler matches a "/start"-shaped update —
     no ~filters.COMMAND exclusion silently dropping command texts.
  3. Inline-keyboard plumbing survives: CallbackQueryHandler registered,
     keyboard builders emit callback_data routed by _handle_callback.
  4. Selector respects snoozed sources (proactive snooze state survives the
     command removal — it is written by set_proactive_source now).
"""
from __future__ import annotations

import datetime as _dt
import importlib
import json
import os
from pathlib import Path

import pytest

from storage import db

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    """Fresh per-test DB."""
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield


def _build_app():
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake:token")
    os.environ.setdefault("OWNER_TELEGRAM_ID", "12345")
    from agents.telegram_bridge import build_application
    return build_application()


def _command_shaped_update(text: str = "/start"):
    """A real telegram.Update carrying a command-shaped text message,
    including the BOT_COMMAND entity a real client would attach."""
    from telegram import Chat, Message, MessageEntity, Update, User
    msg = Message(
        message_id=1,
        date=_dt.datetime.now(_dt.UTC),
        chat=Chat(id=12345, type="private"),
        from_user=User(id=12345, first_name="owner", is_bot=False),
        text=text,
        entities=[
            MessageEntity(
                type=MessageEntity.BOT_COMMAND, offset=0, length=len(text),
            )
        ],
    )
    return Update(update_id=1, message=msg)


# ---------------------------------------------------------------------------
# 1. zero CommandHandler instances
# ---------------------------------------------------------------------------

def test_build_application_registers_zero_command_handlers():
    from telegram.ext import CommandHandler
    app = _build_app()
    offenders = [
        type(h).__name__
        for handlers in app.handlers.values()
        for h in handlers
        if isinstance(h, CommandHandler)
    ]
    assert not offenders, (
        f"Phase 5b contract: zero slash-commands — found CommandHandler(s): "
        f"{offenders}"
    )


# ---------------------------------------------------------------------------
# 2. "/start"-shaped text routes to handle_message (conversation contract)
# ---------------------------------------------------------------------------

def test_message_handler_accepts_command_shaped_text():
    """The TEXT message handler must match a /start update — if it carried a
    ~filters.COMMAND exclusion, command-shaped texts would be DROPPED
    silently instead of becoming a conversational turn."""
    from telegram.ext import MessageHandler

    from agents.telegram_bridge import handle_message

    app = _build_app()
    text_handlers = [
        h
        for handlers in app.handlers.values()
        for h in handlers
        if isinstance(h, MessageHandler) and h.callback is handle_message
    ]
    assert len(text_handlers) == 1, "expected exactly one handle_message handler"

    update = _command_shaped_update("/start")
    assert text_handlers[0].check_update(update), (
        "handle_message's filter rejected a '/start'-shaped update — "
        "command texts must route to conversation, not be dropped"
    )


def test_message_handler_accepts_plain_text():
    """Sanity: plain (non-command) text still matches."""
    from telegram import Chat, Message, Update, User
    from telegram.ext import MessageHandler

    from agents.telegram_bridge import handle_message

    app = _build_app()
    handler = next(
        h
        for handlers in app.handlers.values()
        for h in handlers
        if isinstance(h, MessageHandler) and h.callback is handle_message
    )
    msg = Message(
        message_id=2,
        date=_dt.datetime.now(_dt.UTC),
        chat=Chat(id=12345, type="private"),
        from_user=User(id=12345, first_name="owner", is_bot=False),
        text="hey, what are my reminders?",
    )
    assert handler.check_update(Update(update_id=2, message=msg))


# ---------------------------------------------------------------------------
# 3. inline-keyboard plumbing survives
# ---------------------------------------------------------------------------

def test_callback_query_handler_registered():
    from telegram.ext import CallbackQueryHandler
    app = _build_app()
    cq = [
        h
        for handlers in app.handlers.values()
        for h in handlers
        if isinstance(h, CallbackQueryHandler)
    ]
    assert len(cq) == 1, "inline keyboards need the CallbackQueryHandler route"


def test_keyboard_builders_emit_routed_callback_data():
    """Keyboard builders survive the command purge and their callback_data
    namespaces are ones _handle_callback routes (appr/reminder/checkin/pro)."""
    from agents.telegram_bridge import (
        _kb_approval,
        _kb_checkin_status,
        _kb_reminder,
    )
    routed = {"appr", "checkin", "reminder", "pro"}
    for kb in (_kb_approval(7), _kb_checkin_status(), _kb_reminder(7)):
        for row in kb.inline_keyboard:
            for btn in row:
                ns = (btn.callback_data or "").split(":")[0]
                assert ns in routed, (
                    f"keyboard button {btn.callback_data!r} has no callback route"
                )


# ---------------------------------------------------------------------------
# 4. selector respects snoozed sources
# ---------------------------------------------------------------------------

def test_selector_skips_snoozed():
    """Sources in the snooze map are excluded even when enabled."""
    from types import SimpleNamespace

    # Write a snooze entry that expires 1 hour from now
    future_iso = (
        _dt.datetime.now(_dt.UTC) + _dt.timedelta(hours=1)
    ).isoformat()
    db.runtime_set("proactive_snooze_until", json.dumps({"wiki_new_file": future_iso}))

    from agents.engagement.selector import select
    from agents.engagement.triggers import TriggerCandidate

    candidate = TriggerCandidate(
        source="wiki_new_file",
        pool="user_anchored",
        pattern="notify",
        payload={},
        dedup_key="test-dedup",
        decay_at=_dt.datetime.now(_dt.UTC) + _dt.timedelta(hours=2),
        novelty=1.0,
        actionability=1.0,
        confidence=1.0,
    )
    ctx = SimpleNamespace(
        now_local=_dt.datetime.now(_dt.UTC),
        mood="focused",
        enabled_sources={"wiki_new_file"},
        pool_caps={"user_anchored": True},
        source_response_rate={},
        last_send_per_source={},
    )
    result = select([candidate], ctx)
    assert result is None, "snoozed source should not be selected"
