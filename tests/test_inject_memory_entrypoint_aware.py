"""Phase 13.1 (Stream K) — regression: inject_memory is entrypoint-aware.

K-1 fix: _build_options now accepts inject_memory_enabled: bool = True.
When False, no UserPromptSubmit hook is registered, so run_internal_control
calls cannot waste tokens on persona-memory injection AND cannot race the
pending_surfaced_*_ids runtime_state keys that a concurrent user turn is
about to commit.

Tests:
  - _build_options(inject_memory_enabled=True)  → UserPromptSubmit present
  - _build_options(inject_memory_enabled=False) → UserPromptSubmit absent
  - run_internal_control calls _invoke_sdk with inject_memory_enabled=False
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from agents import config


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    config.reload()
    yield


def _extract_hooks_dict(options) -> dict:
    """Pull the hooks dict from a ClaudeAgentOptions-like object.

    ClaudeAgentOptions may store hooks under .hooks or expose it differently
    depending on the SDK version. We handle both attribute and dict access.
    """
    hooks = getattr(options, "hooks", None)
    if hooks is None:
        # Fall back: see if it's a plain dict we can check directly
        hooks = {}
    return hooks if isinstance(hooks, dict) else {}


def test_build_options_with_memory_enabled_has_user_prompt_submit():
    """When inject_memory_enabled=True (default), UserPromptSubmit is registered."""
    from agents.runtime import _build_options

    opts = _build_options(resume=None, inject_memory_enabled=True)
    hooks = _extract_hooks_dict(opts)
    assert "UserPromptSubmit" in hooks, (
        "inject_memory_enabled=True must register a UserPromptSubmit hook; "
        f"got hooks keys: {list(hooks.keys())}"
    )


def test_build_options_with_memory_disabled_has_no_user_prompt_submit():
    """When inject_memory_enabled=False, no UserPromptSubmit hook is registered."""
    from agents.runtime import _build_options

    opts = _build_options(resume=None, inject_memory_enabled=False)
    hooks = _extract_hooks_dict(opts)
    assert "UserPromptSubmit" not in hooks, (
        "inject_memory_enabled=False must NOT register a UserPromptSubmit hook; "
        f"got hooks keys: {list(hooks.keys())}"
    )


def test_build_options_default_enables_memory():
    """Default call (no inject_memory_enabled kwarg) must include UserPromptSubmit."""
    from agents.runtime import _build_options

    opts = _build_options(resume=None)
    hooks = _extract_hooks_dict(opts)
    assert "UserPromptSubmit" in hooks, (
        "Default _build_options() must register UserPromptSubmit hook"
    )


@pytest.mark.asyncio
async def test_run_internal_control_invokes_sdk_without_memory_hook(monkeypatch):
    """run_internal_control must pass inject_memory_enabled=False to _invoke_sdk."""
    import agents.runtime as runtime_mod

    captured_kwargs: list[dict] = []

    async def fake_invoke_sdk(prompt, *, inject_memory_enabled=True, **kwargs) -> str:
        captured_kwargs.append({"inject_memory_enabled": inject_memory_enabled})
        return "ok"

    monkeypatch.setattr(runtime_mod, "_invoke_sdk", fake_invoke_sdk)

    result = await runtime_mod.run_internal_control("test prompt")

    assert result == "ok"
    assert len(captured_kwargs) == 1, "Expected exactly one _invoke_sdk call"
    assert captured_kwargs[0]["inject_memory_enabled"] is False, (
        f"run_internal_control must pass inject_memory_enabled=False, "
        f"got: {captured_kwargs[0]}"
    )


@pytest.mark.asyncio
async def test_run_user_turn_invokes_sdk_with_memory_hook(monkeypatch):
    """run_user_turn must pass inject_memory_enabled=True (default)."""
    import agents.runtime as runtime_mod
    from storage import db

    captured_kwargs: list[dict] = []

    async def fake_invoke_sdk(prompt, *, inject_memory_enabled=True, **kwargs) -> str:
        captured_kwargs.append({"inject_memory_enabled": inject_memory_enabled})
        return "reply"

    monkeypatch.setattr(runtime_mod, "_invoke_sdk", fake_invoke_sdk)
    # Stub lock acquisition
    monkeypatch.setattr(db, "get_session_id", lambda: None)

    result = await runtime_mod.run_user_turn("hello")

    assert result == "reply"
    assert len(captured_kwargs) == 1
    assert captured_kwargs[0]["inject_memory_enabled"] is True, (
        f"run_user_turn must pass inject_memory_enabled=True (default), "
        f"got: {captured_kwargs[0]}"
    )


@pytest.mark.asyncio
async def test_run_visible_proactive_invokes_sdk_with_memory_hook(monkeypatch):
    """run_visible_proactive must pass inject_memory_enabled=True (default)."""
    import agents.runtime as runtime_mod
    from storage import db

    captured_kwargs: list[dict] = []

    async def fake_invoke_sdk(prompt, *, inject_memory_enabled=True, **kwargs) -> str:
        captured_kwargs.append({"inject_memory_enabled": inject_memory_enabled})
        return "heartbeat text"

    monkeypatch.setattr(runtime_mod, "_invoke_sdk", fake_invoke_sdk)
    monkeypatch.setattr(db, "get_session_id", lambda: None)

    result = await runtime_mod.run_visible_proactive("seed prompt")

    assert result == "heartbeat text"
    assert len(captured_kwargs) == 1
    assert captured_kwargs[0]["inject_memory_enabled"] is True, (
        f"run_visible_proactive must pass inject_memory_enabled=True, "
        f"got: {captured_kwargs[0]}"
    )
