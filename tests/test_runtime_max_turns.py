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
    import inspect
    sig = inspect.signature(runtime._run_query)
    default = sig.parameters["max_turns"].default
    assert default == runtime.DEFAULT_MAX_TURNS


def test_persona_text_carries_substituted_budget():
    """_persona() should embed the live DEFAULT_MAX_TURNS via .replace()."""
    text = runtime._persona()
    assert str(runtime.DEFAULT_MAX_TURNS) in text
    # And no unresolved {max_turns} placeholder.
    assert "{max_turns}" not in text
