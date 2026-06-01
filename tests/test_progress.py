"""tests/test_progress.py — progress @tool rate limiter + mode detection.

Tests cover:
  1. Auto-mode: short + no-surprise → typing; long or surprise → text.
  2. Rate cap: 4 text emissions per turn, 5th is dropped.
  3. Gap guard: emit within 1.5 s is dropped.
  4. Single-step guard: _is_single_step_turn() → emit skipped.
  5. Typing actions bypass cap and gap.
  6. Empty message → skipped with no error.

Import strategy: use importlib.import_module to get the module object
(not the SdkMcpTool that `from tools.runtime.progress import progress` would give).
Invoke the tool via ``pm.progress.handler(args)`` — same as SDK runtime does.
"""
from __future__ import annotations

import importlib
import time

import pytest

# ---- helpers ----

def _get_pm():
    """Return the tools.runtime.progress module (not the wrapped tool)."""
    return importlib.import_module("tools.runtime.progress")


def _reset_state():
    """Reset the module-level ContextVar so each test starts clean."""
    pm = _get_pm()
    pm._PROGRESS_STATE.set({})


async def _call(args):
    """Invoke the progress tool handler."""
    pm = _get_pm()
    return await pm.progress.handler(args)


# ---- mode detection ----

@pytest.mark.asyncio
async def test_auto_mode_short_no_surprise_sends_typing(monkeypatch):
    _reset_state()
    pm = _get_pm()
    sent_typing: list = []
    sent_text: list = []

    async def fake_typing(chat_id): sent_typing.append(chat_id)
    async def fake_text(chat_id, msg): sent_text.append(msg)

    monkeypatch.setattr(pm, "_send_typing", fake_typing)
    monkeypatch.setattr(pm, "_send_text", fake_text)
    monkeypatch.setattr(pm, "_get_chat_id", lambda: 123)
    monkeypatch.setattr(pm, "_is_single_step_turn", lambda: False)

    result = await _call({"message": "fetching email...", "mode": "auto"})
    assert "typing" in result["content"][0]["text"]
    assert sent_typing == [123]
    assert sent_text == []


@pytest.mark.asyncio
async def test_auto_mode_long_sends_text(monkeypatch):
    _reset_state()
    pm = _get_pm()
    sent_text: list = []

    async def fake_typing(chat_id): pass
    async def fake_text(chat_id, msg): sent_text.append(msg)

    monkeypatch.setattr(pm, "_send_typing", fake_typing)
    monkeypatch.setattr(pm, "_send_text", fake_text)
    monkeypatch.setattr(pm, "_get_chat_id", lambda: 123)
    monkeypatch.setattr(pm, "_is_single_step_turn", lambda: False)

    long_msg = "a" * 61
    result = await _call({"message": long_msg, "mode": "auto"})
    assert "text progress sent" in result["content"][0]["text"]
    assert sent_text == [long_msg]


@pytest.mark.asyncio
async def test_auto_mode_surprise_flag_forces_text(monkeypatch):
    _reset_state()
    pm = _get_pm()
    sent_text: list = []

    async def fake_typing(chat_id): pass
    async def fake_text(chat_id, msg): sent_text.append(msg)

    monkeypatch.setattr(pm, "_send_typing", fake_typing)
    monkeypatch.setattr(pm, "_send_text", fake_text)
    monkeypatch.setattr(pm, "_get_chat_id", lambda: 123)
    monkeypatch.setattr(pm, "_is_single_step_turn", lambda: False)

    result = await _call({"message": "oops", "mode": "auto", "surprise": True})
    assert "text progress sent" in result["content"][0]["text"]
    assert sent_text == ["oops"]


# ---- rate cap ----

@pytest.mark.asyncio
async def test_text_cap_blocks_fifth_emission(monkeypatch):
    _reset_state()
    pm = _get_pm()
    sent_text: list = []
    _clock = [1000.0]

    # Advance time by 2s on each call so gap check always passes.
    def fake_mono():
        v = _clock[0]
        _clock[0] += 2.0
        return v

    async def fake_typing(chat_id): pass
    async def fake_text(chat_id, msg): sent_text.append(msg)

    monkeypatch.setattr(pm, "_send_typing", fake_typing)
    monkeypatch.setattr(pm, "_send_text", fake_text)
    monkeypatch.setattr(pm, "_get_chat_id", lambda: 123)
    monkeypatch.setattr(pm, "_is_single_step_turn", lambda: False)
    monkeypatch.setattr(time, "monotonic", fake_mono)

    for i in range(4):
        r = await _call({"message": f"step {i}", "mode": "text"})
        assert "text progress sent" in r["content"][0]["text"]

    # 5th should be capped
    r5 = await _call({"message": "step 4", "mode": "text"})
    assert "cap reached" in r5["content"][0]["text"]
    assert len(sent_text) == 4


# ---- gap guard ----

@pytest.mark.asyncio
async def test_gap_guard_blocks_rapid_emit(monkeypatch):
    _reset_state()
    pm = _get_pm()
    sent_text: list = []
    _clock = [1000.0]

    def fake_mono(): return _clock[0]

    async def fake_typing(chat_id): pass
    async def fake_text(chat_id, msg): sent_text.append(msg)

    monkeypatch.setattr(pm, "_send_typing", fake_typing)
    monkeypatch.setattr(pm, "_send_text", fake_text)
    monkeypatch.setattr(pm, "_get_chat_id", lambda: 123)
    monkeypatch.setattr(pm, "_is_single_step_turn", lambda: False)
    monkeypatch.setattr(time, "monotonic", fake_mono)

    r1 = await _call({"message": "first", "mode": "text"})
    assert "text progress sent" in r1["content"][0]["text"]

    # advance only 0.5 s — below 1.5 s gap
    _clock[0] = 1000.5
    r2 = await _call({"message": "second", "mode": "text"})
    assert "gap too small" in r2["content"][0]["text"]
    assert len(sent_text) == 1


# ---- single-step guard ----

@pytest.mark.asyncio
async def test_single_step_turn_skips_all(monkeypatch):
    _reset_state()
    pm = _get_pm()
    sent_typing: list = []
    sent_text: list = []

    async def fake_typing(chat_id): sent_typing.append(chat_id)
    async def fake_text(chat_id, msg): sent_text.append(msg)

    monkeypatch.setattr(pm, "_send_typing", fake_typing)
    monkeypatch.setattr(pm, "_send_text", fake_text)
    monkeypatch.setattr(pm, "_get_chat_id", lambda: 123)
    monkeypatch.setattr(pm, "_is_single_step_turn", lambda: True)

    r = await _call({"message": "anything", "mode": "auto"})
    assert "single-step" in r["content"][0]["text"]
    assert sent_typing == []
    assert sent_text == []


# ---- typing bypasses cap ----

@pytest.mark.asyncio
async def test_typing_not_counted_in_cap(monkeypatch):
    _reset_state()
    pm = _get_pm()
    sent_typing: list = []
    sent_text: list = []

    async def fake_typing(chat_id): sent_typing.append(chat_id)
    async def fake_text(chat_id, msg): sent_text.append(msg)

    monkeypatch.setattr(pm, "_send_typing", fake_typing)
    monkeypatch.setattr(pm, "_send_text", fake_text)
    monkeypatch.setattr(pm, "_get_chat_id", lambda: 123)
    monkeypatch.setattr(pm, "_is_single_step_turn", lambda: False)
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)

    # 10 typing actions — all pass, none counted
    for _ in range(10):
        r = await _call({"message": "typing...", "mode": "typing"})
        assert "typing action sent" in r["content"][0]["text"]

    assert len(sent_typing) == 10
    # Now a text emit should still be allowed (count=0)
    r = await _call({"message": "result ready", "mode": "text"})
    assert "text progress sent" in r["content"][0]["text"]


# ---- empty message ----

@pytest.mark.asyncio
async def test_empty_message_skipped(monkeypatch):
    _reset_state()
    pm = _get_pm()
    monkeypatch.setattr(pm, "_is_single_step_turn", lambda: False)
    monkeypatch.setattr(pm, "_get_chat_id", lambda: 123)

    r = await _call({"message": ""})
    assert "skipped" in r["content"][0]["text"]
