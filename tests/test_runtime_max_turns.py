"""Regression pin for DEFAULT_MAX_TURNS.

Last review caught that a silent revert from 4 to 15 would cost 5x token
budget per turn. This test fails loud if someone changes the constant
or unmoors it from the chat-path entry points.
"""
from __future__ import annotations

from agents import runtime


def test_default_max_turns_is_four():
    assert runtime.DEFAULT_MAX_TURNS == 4


def test_build_options_default_uses_constant():
    """_build_options' max_turns parameter default must == DEFAULT_MAX_TURNS."""
    import inspect
    sig = inspect.signature(runtime._build_options)
    default = sig.parameters["max_turns"].default
    assert default == runtime.DEFAULT_MAX_TURNS


def test_run_query_default_uses_constant():
    """Stream C split _run_query into _invoke_sdk + 3 entrypoints.
    Pin that _invoke_sdk's max_turns default == DEFAULT_MAX_TURNS."""
    import inspect
    sig = inspect.signature(runtime._invoke_sdk)
    default = sig.parameters["max_turns"].default
    assert default == runtime.DEFAULT_MAX_TURNS


def test_persona_text_has_no_substitution_placeholders():
    """_persona() is cached verbatim from assets/PERSONA.md. Per-turn values
    (max_turns, time) live in the # now block, not in the cached persona."""
    text = runtime._persona()
    assert "{max_turns}" not in text


def test_now_block_carries_live_max_turns():
    """_format_now must surface the current DEFAULT_MAX_TURNS so Hikari
    sees her budget per turn without busting the prompt cache."""
    from agents.hooks import _format_now
    now_block = _format_now()
    assert f"max_turns: {runtime.DEFAULT_MAX_TURNS}" in now_block
