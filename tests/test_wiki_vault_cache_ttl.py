"""Tests for the TTL cache in tools/wiki/_shared.py — vault rebuild and invalidation."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_vault_cache():
    """Ensure _VAULT_CACHE is cleared before and after each test."""
    import tools.wiki._shared as ws
    ws._VAULT_CACHE = None
    yield
    ws._VAULT_CACHE = None


def _make_fake_vault():
    mock_vault = MagicMock()
    mock_vault.md_file_index = {}
    mock_vault.backlinks_index = {}
    mock_vault.connect.return_value = mock_vault
    mock_vault.gather.return_value = mock_vault
    return mock_vault


def test_vault_cache_expires_after_ttl(monkeypatch):
    import tools.wiki._shared as ws

    call_count = 0
    vaults = [_make_fake_vault(), _make_fake_vault()]

    def fake_vault_constructor(root):
        nonlocal call_count
        v = vaults[call_count]
        call_count += 1
        return v

    monkeypatch.setattr(ws, "_VAULT_TTL_SEC", 0.05)  # 50ms TTL

    with patch("tools.wiki._shared.Vault", side_effect=fake_vault_constructor):
        first = ws._vault()
        # Still within TTL — should return same cached instance
        second = ws._vault()
        assert first is second
        assert call_count == 1

        # Wait for TTL to expire
        time.sleep(0.1)

        third = ws._vault()
        assert call_count == 2
        assert third is not first


def test_invalidate_vault_forces_rebuild(monkeypatch):
    import tools.wiki._shared as ws

    call_count = 0
    vaults = [_make_fake_vault(), _make_fake_vault()]

    def fake_vault_constructor(root):
        nonlocal call_count
        v = vaults[call_count]
        call_count += 1
        return v

    with patch("tools.wiki._shared.Vault", side_effect=fake_vault_constructor):
        first = ws._vault()
        assert call_count == 1

        # Invalidate — forces rebuild on next call regardless of TTL
        ws.invalidate_vault()
        assert ws._VAULT_CACHE is None

        second = ws._vault()
        assert call_count == 2
        assert second is not first
