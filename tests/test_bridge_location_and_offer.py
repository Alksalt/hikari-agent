"""FIX 6 + FIX 11 bridge fixes.

FIX 6: the plain-message location handler must be scoped to UpdateType.MESSAGE
so live-location edited_message ticks fall through to handle_edited_location
(PTB runs only the first matching handler per group).

FIX 11: _cb_offer must not fail silently — a respond() error surfaces the same
in-voice fallback the text path uses.
"""
from __future__ import annotations

import importlib
from datetime import UTC, datetime

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:dummy")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    yield


def _msg_with_location(edited: bool):
    from telegram import Chat, Location, Message, Update, User
    loc = Location(longitude=1.0, latitude=2.0)
    chat = Chat(id=1, type="private")
    user = User(id=12345, first_name="u", is_bot=False)
    msg = Message(
        message_id=1, date=datetime.now(UTC), chat=chat,
        from_user=user, location=loc,
    )
    if edited:
        return Update(update_id=2, edited_message=msg)
    return Update(update_id=1, message=msg)


def _find_handler(app, callback):
    for handlers in app.handlers.values():
        for h in handlers:
            if getattr(h, "callback", None) is callback:
                return h
    return None


def test_location_handler_ignores_edited_updates():
    """The handle_location registration must reject edited_message updates so
    they fall through to handle_edited_location."""
    from agents import telegram_bridge as tb
    app = tb.build_application()

    loc_handler = _find_handler(app, tb.handle_location)
    edit_handler = _find_handler(app, tb.handle_edited_location)
    assert loc_handler is not None
    assert edit_handler is not None

    plain = _msg_with_location(edited=False)
    edited = _msg_with_location(edited=True)

    # Plain message location → matched by handle_location, not by the edit handler.
    assert loc_handler.check_update(plain)
    assert not edit_handler.check_update(plain)

    # Edited location tick → NOT swallowed by handle_location; the edit handler takes it.
    assert not loc_handler.check_update(edited)
    assert edit_handler.check_update(edited)


@pytest.mark.asyncio
async def test_cb_offer_surfaces_fallback_on_respond_failure(monkeypatch):
    from agents import capability_offers as offers_mod
    from agents import telegram_bridge as tb

    monkeypatch.setattr(offers_mod, "catalog_entry", lambda oid: {"phrase": "do the thing"})

    async def _boom(*a, **kw):
        raise RuntimeError("brain exploded")

    monkeypatch.setattr(tb, "respond", _boom)

    sent: list[str] = []

    class _Bot:
        async def send_message(self, chat_id, text):
            sent.append(text)

    await tb._cb_offer(_Bot(), 12345, row_id=1, offer_id="some_offer")

    assert any("brain hit a wall" in s for s in sent), (
        "respond() failure in _cb_offer must surface the in-voice fallback"
    )
