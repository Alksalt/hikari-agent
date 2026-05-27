"""Tests for Notion keychain injection warning.

Verifies that when auth.notion._load_token raises OR returns None/empty token,
the runtime logs a WARNING-level message containing both:
  - "notion token not loaded"
  - "scripts.auth notion grant"
"""
from __future__ import annotations

import logging
import sys
import types


def _install_notion_mock(monkeypatch, load_token_fn):
    """Install a mock auth.notion module with the given _load_token implementation."""
    if "auth.notion" not in sys.modules:
        auth_pkg = sys.modules.get("auth") or types.ModuleType("auth")
        notion_mod = types.ModuleType("auth.notion")
        notion_mod._load_token = load_token_fn  # type: ignore[attr-defined]
        sys.modules.setdefault("auth", auth_pkg)
        sys.modules["auth.notion"] = notion_mod
    else:
        monkeypatch.setattr("auth.notion._load_token", load_token_fn)


def _collect_warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_notion_token_failure_emits_warning(monkeypatch, caplog):
    """Patch _load_token to raise; call _inject_keychain_tokens_to_env;
    assert a WARNING with both 'notion token not loaded' and
    'scripts.auth notion grant' was emitted."""
    import agents.runtime as runtime_mod

    def _raise():
        raise RuntimeError("keychain unavailable")

    _install_notion_mock(monkeypatch, _raise)

    # Ensure NOTION_TOKEN is not already set.
    monkeypatch.delenv("NOTION_TOKEN", raising=False)

    with caplog.at_level(logging.WARNING, logger="agents.runtime"):
        runtime_mod._inject_keychain_tokens_to_env()

    warning_messages = _collect_warnings(caplog)
    assert any("notion token not loaded" in m for m in warning_messages), (
        f"Expected 'notion token not loaded' in WARNING logs, got: {warning_messages}"
    )
    assert any("scripts.auth notion grant" in m for m in warning_messages), (
        f"Expected 'scripts.auth notion grant' in WARNING logs, got: {warning_messages}"
    )


def test_notion_token_none_return_emits_warning(monkeypatch, caplog):
    """Patch _load_token to return None (empty keychain); call
    _inject_keychain_tokens_to_env; assert a WARNING with both
    'notion token not loaded' and 'scripts.auth notion grant' was emitted."""
    import agents.runtime as runtime_mod

    def _return_none():
        return None

    _install_notion_mock(monkeypatch, _return_none)

    # Ensure NOTION_TOKEN is not already set.
    monkeypatch.delenv("NOTION_TOKEN", raising=False)

    with caplog.at_level(logging.WARNING, logger="agents.runtime"):
        runtime_mod._inject_keychain_tokens_to_env()

    warning_messages = _collect_warnings(caplog)
    assert any("notion token not loaded" in m for m in warning_messages), (
        f"Expected 'notion token not loaded' in WARNING logs, got: {warning_messages}"
    )
    assert any("scripts.auth notion grant" in m for m in warning_messages), (
        f"Expected 'scripts.auth notion grant' in WARNING logs, got: {warning_messages}"
    )
