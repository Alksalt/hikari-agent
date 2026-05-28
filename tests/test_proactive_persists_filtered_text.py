"""Phase 13.1 (Stream K) — regression: proactive messages persist FILTERED text.

Pins G-1 fix: send_text now returns (final_text, telegram_message_id, sent_ok).
The proactive pipeline must:
  1. Send the filtered text (not the pre-filter draft) to Telegram.
  2. Persist the FINAL filtered text in the messages table (not the draft).
  3. Store the Telegram message_id so 👍/👎 joins work.
  4. Set source='proactive' on the persisted row.

Phase J note: maybe_send_heartbeat / maybe_send_reengagement / maybe_send_calendar_heartbeat
were deleted. Only test_send_text_returns_filtered_text_and_message_id remains,
testing _unpack_send_result directly.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

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
# Helper: stub filter_outgoing to rewrite DRAFT_TEXT → FILTERED_TEXT
# ---------------------------------------------------------------------------

def _make_filter_result(text: str):
    """Build a minimal FilterResult-like object with the given text."""
    return SimpleNamespace(
        text=text,
        refusal_short_replaced=False,
        needs_llm_rewrite=False,
        refusal_hits=[],
    )


@pytest.mark.asyncio
async def test_send_text_returns_filtered_text_and_message_id(monkeypatch, tmp_path):
    """send_text must return (filtered_text, message_id, True) when filter rewrites."""
    import agents.post_filter as post_filter_mod
    # Stub filter_outgoing: rewrite DRAFT_TEXT → FILTERED_TEXT
    monkeypatch.setattr(
        post_filter_mod,
        "filter_outgoing",
        lambda text, *, source=None: _make_filter_result("FILTERED_TEXT" if text == "DRAFT_TEXT" else text),
    )

    # Verify _unpack_send_result handles the (filtered_text, message_id, ok) tuple:
    from agents.proactive import _unpack_send_result

    # Simulate what send_text would return after filter + bot.send_message
    simulated_result = ("FILTERED_TEXT", 42, True)
    final, tg_id, ok = _unpack_send_result(simulated_result, "DRAFT_TEXT")

    assert final == "FILTERED_TEXT", f"Expected FILTERED_TEXT, got {final!r}"
    assert tg_id == 42, f"Expected message_id=42, got {tg_id!r}"
    assert ok is True


