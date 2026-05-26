"""tests/test_progress_tool.py — progress tool rate-limit + bot routing.

Tests the progress tool at the bot-call layer: mocks ``bot.send_chat_action``
and ``bot.send_message`` (rather than the internal ``_send_typing``/
``_send_text`` helpers that test_progress.py uses).

Cases:
  1.  Rate limit: 5th text call within a turn returns "cap reached" (no-op).
  2.  Min gap 1.5s: second text call within 1.5 s is dropped.
  3.  Single-step mode: all calls skipped via _is_single_step_turn().
  4.  Surprise=True forces text mode (bypasses auto→typing selection).
  5.  sub-2s message in auto-mode → send_chat_action("typing") called.
  6.  >2s message (or text mode) → send_message called, not send_chat_action.
  7.  Empty message → skipped, neither bot method called.
  8.  Typing mode never counts toward the text rate cap.
  9.  Gap guard: emit at t=0, emit at t=0.5 → dropped; emit at t=2.0 → ok.
  10. chat_id=None (owner_id not set) → no bot calls but tool returns ok.
"""
from __future__ import annotations

import importlib
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pm():
    """Fresh module reference for tools.runtime.progress."""
    return importlib.import_module("tools.runtime.progress")


def _reset():
    """Clear ContextVar between tests."""
    _pm()._PROGRESS_STATE.set({})


def _make_bot():
    """Return a mock bot with send_chat_action and send_message as AsyncMocks."""
    bot = MagicMock()
    bot.send_chat_action = AsyncMock()
    bot.send_message = AsyncMock()
    return bot


async def _call(args):
    return await _pm().progress.handler(args)


# ---------------------------------------------------------------------------
# 1. Rate cap: 5th text call is dropped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rate_cap_fifth_call_dropped(monkeypatch):
    """4 text calls succeed; the 5th is silently dropped."""
    _reset()
    pm = _pm()
    bot = _make_bot()

    _clock = [1000.0]
    def fake_mono():
        v = _clock[0]
        _clock[0] += 2.0
        return v

    monkeypatch.setattr(pm, "_get_chat_id", lambda: 1)
    monkeypatch.setattr(pm, "_is_single_step_turn", lambda: False)
    monkeypatch.setattr(time, "monotonic", fake_mono)

    with patch("agents.telegram_bridge._get_current_bot", return_value=bot):
        for i in range(4):
            r = await _call({"message": f"step {i}", "mode": "text"})
            assert "text progress sent" in r["content"][0]["text"]

        r5 = await _call({"message": "step 4", "mode": "text"})

    assert "cap reached" in r5["content"][0]["text"]
    assert bot.send_message.await_count == 4


# ---------------------------------------------------------------------------
# 2. Min-gap 1.5s: second call within 1.5s is dropped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_min_gap_rapid_second_dropped(monkeypatch):
    """Second text emit within 1.5 s is dropped."""
    _reset()
    pm = _pm()
    bot = _make_bot()

    _clock = [1000.0]
    def fake_mono():
        return _clock[0]

    monkeypatch.setattr(pm, "_get_chat_id", lambda: 1)
    monkeypatch.setattr(pm, "_is_single_step_turn", lambda: False)
    monkeypatch.setattr(time, "monotonic", fake_mono)

    with patch("agents.telegram_bridge._get_current_bot", return_value=bot):
        r1 = await _call({"message": "first emit", "mode": "text"})
        assert "text progress sent" in r1["content"][0]["text"]

        # 0.5 s later — gap is too small
        _clock[0] = 1000.5
        r2 = await _call({"message": "second emit", "mode": "text"})

    assert "gap too small" in r2["content"][0]["text"]
    assert bot.send_message.await_count == 1


# ---------------------------------------------------------------------------
# 3. Single-step mode skips all calls
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_step_mode_skips_all(monkeypatch):
    """_is_single_step_turn() → True skips all emissions."""
    _reset()
    pm = _pm()
    bot = _make_bot()

    monkeypatch.setattr(pm, "_get_chat_id", lambda: 1)
    monkeypatch.setattr(pm, "_is_single_step_turn", lambda: True)

    with patch("agents.telegram_bridge._get_current_bot", return_value=bot):
        r = await _call({"message": "doing stuff", "mode": "auto"})

    assert "single-step" in r["content"][0]["text"]
    assert bot.send_chat_action.await_count == 0
    assert bot.send_message.await_count == 0


# ---------------------------------------------------------------------------
# 4. Surprise=True forces text mode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_surprise_flag_forces_text_mode(monkeypatch):
    """A short message with surprise=True is sent as text, not typing."""
    _reset()
    pm = _pm()
    bot = _make_bot()

    monkeypatch.setattr(pm, "_get_chat_id", lambda: 1)
    monkeypatch.setattr(pm, "_is_single_step_turn", lambda: False)
    monkeypatch.setattr(time, "monotonic", lambda: 2000.0)

    with patch("agents.telegram_bridge._get_current_bot", return_value=bot):
        r = await _call({"message": "oops", "mode": "auto", "surprise": True})

    assert "text progress sent" in r["content"][0]["text"]
    assert bot.send_message.await_count == 1
    assert bot.send_chat_action.await_count == 0


# ---------------------------------------------------------------------------
# 5. Short auto-mode → send_chat_action ("typing") called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_short_auto_mode_calls_send_chat_action(monkeypatch):
    """Short message (<60 chars, no surprise) in auto mode → typing indicator."""
    _reset()
    pm = _pm()
    bot = _make_bot()

    monkeypatch.setattr(pm, "_get_chat_id", lambda: 1)
    monkeypatch.setattr(pm, "_is_single_step_turn", lambda: False)

    with patch("agents.telegram_bridge._get_current_bot", return_value=bot):
        r = await _call({"message": "checking...", "mode": "auto"})

    assert "typing" in r["content"][0]["text"]
    assert bot.send_chat_action.await_count == 1
    assert bot.send_message.await_count == 0


# ---------------------------------------------------------------------------
# 6. Long auto-mode → send_message called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_long_auto_mode_calls_send_message(monkeypatch):
    """Long message (≥60 chars) in auto mode → send_message."""
    _reset()
    pm = _pm()
    bot = _make_bot()

    monkeypatch.setattr(pm, "_get_chat_id", lambda: 1)
    monkeypatch.setattr(pm, "_is_single_step_turn", lambda: False)
    monkeypatch.setattr(time, "monotonic", lambda: 3000.0)

    long_msg = "a" * 61  # above 60-char threshold

    with patch("agents.telegram_bridge._get_current_bot", return_value=bot):
        r = await _call({"message": long_msg, "mode": "auto"})

    assert "text progress sent" in r["content"][0]["text"]
    assert bot.send_message.await_count == 1
    assert bot.send_chat_action.await_count == 0


# ---------------------------------------------------------------------------
# 7. Empty message → skipped, no bot calls
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_message_no_bot_calls(monkeypatch):
    """Empty message → skipped immediately, no bot calls made."""
    _reset()
    pm = _pm()
    bot = _make_bot()

    monkeypatch.setattr(pm, "_is_single_step_turn", lambda: False)
    monkeypatch.setattr(pm, "_get_chat_id", lambda: 1)

    with patch("agents.telegram_bridge._get_current_bot", return_value=bot):
        r = await _call({"message": ""})

    assert "skipped" in r["content"][0]["text"]
    assert bot.send_chat_action.await_count == 0
    assert bot.send_message.await_count == 0


# ---------------------------------------------------------------------------
# 8. Typing mode does not count toward text rate cap
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_typing_mode_not_counted_in_text_cap(monkeypatch):
    """10 typing calls should not affect the text-mode rate cap (still 4 slots)."""
    _reset()
    pm = _pm()
    bot = _make_bot()

    _clock = [5000.0]
    def fake_mono():
        v = _clock[0]
        _clock[0] += 2.0
        return v

    monkeypatch.setattr(pm, "_get_chat_id", lambda: 1)
    monkeypatch.setattr(pm, "_is_single_step_turn", lambda: False)
    monkeypatch.setattr(time, "monotonic", fake_mono)

    with patch("agents.telegram_bridge._get_current_bot", return_value=bot):
        # Send 10 typing actions — should not deplete text cap
        for _ in range(10):
            r = await _call({"message": "activity...", "mode": "typing"})
            assert "typing action sent" in r["content"][0]["text"]

        # Now 4 text emissions should still all succeed
        for i in range(4):
            r = await _call({"message": f"text {i}", "mode": "text"})
            assert "text progress sent" in r["content"][0]["text"], f"text {i} should succeed"

        # 5th text should be capped
        r5 = await _call({"message": "overflow", "mode": "text"})

    assert "cap reached" in r5["content"][0]["text"]
    assert bot.send_chat_action.await_count == 10
    assert bot.send_message.await_count == 4


# ---------------------------------------------------------------------------
# 9. Gap guard three-call sequence: t=0 ok, t=0.5 dropped, t=2.0 ok
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gap_guard_three_call_sequence(monkeypatch):
    """t=100 ok, t=100.5 dropped (< 1.5s gap), t=102.0 ok (>= 1.5s gap).

    Clock must start above 0 because 0.0 is the "never sent" sentinel —
    the gap guard only fires when last_ts > 0.
    """
    _reset()
    pm = _pm()
    bot = _make_bot()

    _clock = [100.0]
    def fake_mono():
        return _clock[0]

    monkeypatch.setattr(pm, "_get_chat_id", lambda: 1)
    monkeypatch.setattr(pm, "_is_single_step_turn", lambda: False)
    monkeypatch.setattr(time, "monotonic", fake_mono)

    with patch("agents.telegram_bridge._get_current_bot", return_value=bot):
        r1 = await _call({"message": "first", "mode": "text"})
        assert "text progress sent" in r1["content"][0]["text"]

        _clock[0] = 100.5  # +0.5s — below 1.5s gap
        r2 = await _call({"message": "second", "mode": "text"})
        assert "gap too small" in r2["content"][0]["text"]

        _clock[0] = 102.0  # +1.5s from first emit — ok
        r3 = await _call({"message": "third", "mode": "text"})
        assert "text progress sent" in r3["content"][0]["text"]

    assert bot.send_message.await_count == 2


# ---------------------------------------------------------------------------
# 10. chat_id=None → no bot calls, tool returns ok
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_chat_id_no_bot_calls(monkeypatch):
    """When _get_chat_id() returns None, no bot calls are made but tool succeeds."""
    _reset()
    pm = _pm()
    bot = _make_bot()

    monkeypatch.setattr(pm, "_get_chat_id", lambda: None)
    monkeypatch.setattr(pm, "_is_single_step_turn", lambda: False)
    monkeypatch.setattr(time, "monotonic", lambda: 9000.0)

    with patch("agents.telegram_bridge._get_current_bot", return_value=bot):
        # typing mode
        r_typing = await _call({"message": "checking", "mode": "typing"})
        assert "typing action sent" in r_typing["content"][0]["text"]

        # text mode
        r_text = await _call({"message": "a" * 61, "mode": "text"})
        assert "text progress sent" in r_text["content"][0]["text"]

    # bot should not have been called since chat_id was None
    assert bot.send_chat_action.await_count == 0
    assert bot.send_message.await_count == 0
