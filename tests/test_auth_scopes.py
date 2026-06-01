"""Phase C — auth/ abstraction + scope-check gate tests.

Covers:
  - MemoryStore set/get/clear roundtrip
  - KeychainStore roundtrip with mocked keyring backend
  - GoogleProvider.current_scopes() — cache miss/hit/network fail
  - Hook _precheck_scopes — shadow/enforce/off modes
  - HIKARI_REQUIRE_KEYCHAIN=1 + simulated ImportError → raises
"""
from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agents import config as agent_config

# ---------------------------------------------------------------------------
# Shared fixture — isolated DB per test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from storage import db
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    agent_config.reload()

    # Reset auth singletons so each test starts clean
    import auth.providers as providers_mod
    import auth.store as store_mod
    store_mod._reset_store()
    providers_mod._reset_providers()
    providers_mod._scope_config = None

    yield

    # Cleanup
    store_mod._reset_store()
    providers_mod._reset_providers()
    providers_mod._scope_config = None


# ===========================================================================
# MemoryStore
# ===========================================================================

class TestMemoryStore:
    def test_set_get_roundtrip(self):
        from auth.store import MemoryStore
        s = MemoryStore()
        s.set("google", "refresh_token", "tok123")
        assert s.get("google", "refresh_token") == "tok123"

    def test_get_missing_returns_none(self):
        from auth.store import MemoryStore
        s = MemoryStore()
        assert s.get("google", "nonexistent") is None

    def test_clear_removes_all_provider_keys(self):
        from auth.store import MemoryStore
        s = MemoryStore()
        s.set("google", "client_id", "cid")
        s.set("google", "client_secret", "csec")
        s.set("notion", "token", "ntok")
        s.clear("google")
        assert s.get("google", "client_id") is None
        assert s.get("google", "client_secret") is None
        # Other provider unaffected
        assert s.get("notion", "token") == "ntok"

    def test_overwrite(self):
        from auth.store import MemoryStore
        s = MemoryStore()
        s.set("google", "k", "v1")
        s.set("google", "k", "v2")
        assert s.get("google", "k") == "v2"


# ===========================================================================
# KeychainStore with mocked keyring
# ===========================================================================

class TestKeychainStore:
    def _mock_keyring(self):
        """Return a fake keyring module backed by a dict."""
        data: dict[tuple[str, str], str] = {}

        class FakeKeyringErrors:
            class KeyringError(Exception):
                pass

        fake = MagicMock()
        fake.errors = FakeKeyringErrors()

        def get_password(service, username):
            return data.get((service, username))

        def set_password(service, username, password):
            data[(service, username)] = password

        def delete_password(service, username):
            keys = [(s, u) for (s, u) in data if s == service]
            for k in keys:
                del data[k]

        fake.get_password = get_password
        fake.set_password = set_password
        fake.delete_password = delete_password
        return fake

    def test_roundtrip(self):
        import auth.store as store_mod
        fake_keyring = self._mock_keyring()
        with patch.dict("sys.modules", {"keyring": fake_keyring}):
            store_mod._reset_store()
            # Construct directly with the patched module
            from auth.store import KeychainStore
            s = KeychainStore.__new__(KeychainStore)
            s._keyring = fake_keyring

            s.set("google", "refresh_token", "rtok")
            assert s.get("google", "refresh_token") == "rtok"

    def test_clear_removes_keys(self):
        fake_keyring = self._mock_keyring()
        from auth.store import KeychainStore
        s = KeychainStore.__new__(KeychainStore)
        s._keyring = fake_keyring
        s.set("google", "client_id", "cid")
        s.clear("google")
        assert s.get("google", "client_id") is None


# ===========================================================================
# default_store — fallback and HIKARI_REQUIRE_KEYCHAIN
# ===========================================================================

class TestDefaultStore:
    def test_falls_back_to_memory_when_keyring_import_errors(self, monkeypatch):
        import auth.store as store_mod
        store_mod._reset_store()
        monkeypatch.delenv("HIKARI_REQUIRE_KEYCHAIN", raising=False)

        # noqa: F821 — __builtins__ is implementation-defined
        original_import = (
            __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__
        )

        def _import_fail(name, *args, **kwargs):
            if name == "keyring":
                raise ImportError("no keyring")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_import_fail):
            store_mod._reset_store()
            # Re-instantiate by monkey-patching KeychainStore.__init__
            with patch.object(store_mod.KeychainStore, "__init__",
                              side_effect=ImportError("no keyring")):
                store_mod._reset_store()
                result = store_mod.default_store()
        assert isinstance(result, store_mod.MemoryStore)

    def test_raises_when_require_keychain_and_keyring_fails(self, monkeypatch):
        import auth.store as store_mod
        store_mod._reset_store()
        monkeypatch.setenv("HIKARI_REQUIRE_KEYCHAIN", "1")

        with patch.object(store_mod.KeychainStore, "__init__",
                          side_effect=ImportError("no keyring")):
            store_mod._reset_store()
            with pytest.raises(RuntimeError, match="HIKARI_REQUIRE_KEYCHAIN"):
                store_mod.default_store()


# ===========================================================================
# GoogleProvider.current_scopes()
# ===========================================================================

class TestGoogleProviderCurrentScopes:
    def _make_provider(self):
        from auth.google import GoogleProvider
        from auth.store import MemoryStore
        store = MemoryStore()
        store.set("google", "client_id", "cid")
        store.set("google", "client_secret", "csec")
        store.set("google", "refresh_token", "rtok")
        return GoogleProvider(store)

    @pytest.mark.asyncio
    async def test_cache_miss_fetches_and_caches(self, monkeypatch):
        from storage import db

        db.runtime_set("auth.google.scopes", None)
        db.runtime_set("auth.google.scopes_checked_at", None)

        provider = self._make_provider()

        class _StubResp:
            def __init__(self, payload):
                self._payload = payload
            def raise_for_status(self): pass
            def json(self): return self._payload

        class _StubAsyncClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, **kw):
                return _StubResp({"access_token": "atok"})
            async def get(self, url, **kw):
                return _StubResp({"scope": "https://mail.google.com/ https://www.googleapis.com/auth/calendar"})

        import auth.google as google_mod
        monkeypatch.setattr(google_mod.httpx, "AsyncClient", _StubAsyncClient)

        scopes = await provider.current_scopes()
        assert "https://mail.google.com/" in scopes
        assert "https://www.googleapis.com/auth/calendar" in scopes
        # Cached
        assert db.runtime_get("auth.google.scopes") is not None

    @pytest.mark.asyncio
    async def test_cache_hit_skips_network(self, monkeypatch):
        from datetime import UTC, datetime

        import auth.google as google_mod
        from storage import db

        db.runtime_set("auth.google.scopes", "https://mail.google.com/")
        db.runtime_set("auth.google.scopes_checked_at", datetime.now(UTC).isoformat())

        provider = self._make_provider()

        call_count = {"n": 0}

        class _StubAsyncClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **kw):
                call_count["n"] += 1
                raise AssertionError("should not hit network")
            async def get(self, *a, **kw):
                call_count["n"] += 1
                raise AssertionError("should not hit network")

        monkeypatch.setattr(google_mod.httpx, "AsyncClient", _StubAsyncClient)

        scopes = await provider.current_scopes()
        assert "https://mail.google.com/" in scopes
        assert call_count["n"] == 0

    @pytest.mark.asyncio
    async def test_network_fail_returns_empty_no_cache(self, monkeypatch):
        import auth.google as google_mod
        from storage import db

        db.runtime_set("auth.google.scopes", None)
        db.runtime_set("auth.google.scopes_checked_at", None)

        provider = self._make_provider()

        class _FailClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **kw):
                raise ConnectionError("network gone")

        monkeypatch.setattr(google_mod.httpx, "AsyncClient", _FailClient)

        scopes = await provider.current_scopes()
        assert scopes == set()
        # Cache NOT written
        assert db.runtime_get("auth.google.scopes") is None


# ===========================================================================
# KeychainStore.clear() includes 'grant'
# ===========================================================================

class TestKeychainStoreClearIncludesGrant:
    def _make_store(self):
        """Return a KeychainStore wired to an in-process dict backend."""
        data: dict[tuple[str, str], str] = {}

        fake = MagicMock()

        def get_password(service, username):
            return data.get((service, username))

        def set_password(service, username, password):
            data[(service, username)] = password

        def delete_password(service, username):
            data.pop((service, username), None)

        fake.get_password = get_password
        fake.set_password = set_password
        fake.delete_password = delete_password

        from auth.store import KeychainStore
        s = KeychainStore.__new__(KeychainStore)
        s._keyring = fake
        return s, data

    def test_clear_deletes_grant_key(self):
        """KeychainStore.clear('google') must remove the 'grant' keychain item."""
        s, data = self._make_store()
        s.set("google", "grant", '{"access_token": "tok"}')
        s.set("google", "client_id", "cid")
        s.clear("google")
        assert s.get("google", "grant") is None
        assert s.get("google", "client_id") is None

    def test_clear_does_not_raise_when_grant_absent(self):
        """clear() with no 'grant' item present must not raise."""
        s, _ = self._make_store()
        s.set("google", "refresh_token", "rtok")
        s.clear("google")  # no 'grant' stored — should complete silently
        assert s.get("google", "refresh_token") is None


# ===========================================================================
# Scope cache flush on revoke() and write_grant_to_keychain()
# ===========================================================================

class TestScopeCacheFlush:
    def _make_provider(self):
        from auth.google import GoogleProvider
        from auth.store import MemoryStore
        store = MemoryStore()
        store.set("google", "client_id", "cid")
        store.set("google", "client_secret", "csec")
        store.set("google", "refresh_token", "rtok")
        return GoogleProvider(store)

    def test_revoke_flushes_scope_cache(self, monkeypatch):
        """revoke() must clear auth.google.scopes and auth.google.scopes_checked_at
        from runtime_state so stale broad scopes cannot survive a narrower re-grant."""
        from datetime import UTC, datetime

        import auth.google as google_mod
        from storage import db

        # Seed the cache as if a broad scope grant was active.
        db.runtime_set("auth.google.scopes", "https://mail.google.com/")
        db.runtime_set("auth.google.scopes_checked_at", datetime.now(UTC).isoformat())

        provider = self._make_provider()

        # Stub the revoke HTTP call so we don't hit the network.
        class _FakeHttpx:
            @staticmethod
            def post(*a, **kw):
                pass

        monkeypatch.setattr(google_mod, "httpx", _FakeHttpx)
        # Stub _httpx import inside revoke() — it re-imports httpx as _httpx.
        import sys
        monkeypatch.setitem(sys.modules, "httpx", _FakeHttpx)

        provider.revoke()

        assert db.runtime_get("auth.google.scopes") is None
        assert db.runtime_get("auth.google.scopes_checked_at") is None

    def test_write_grant_to_keychain_flushes_scope_cache(self):
        """write_grant_to_keychain() must clear the scope runtime cache so a
        re-grant with different scopes is visible immediately."""
        from datetime import UTC, datetime

        from auth import store as store_mod
        from auth.google import write_grant_to_keychain
        from storage import db

        # Seed stale cache.
        db.runtime_set("auth.google.scopes", "https://mail.google.com/")
        db.runtime_set("auth.google.scopes_checked_at", datetime.now(UTC).isoformat())

        # Use MemoryStore so no keyring calls needed.
        store_mod._reset_store()
        mem = store_mod.MemoryStore()
        store_mod._store = mem

        write_grant_to_keychain({
            "client_id": "cid",
            "client_secret": "csec",
            "refresh_token": "rtok",
            "access_token": "atok",
            "scope": "https://www.googleapis.com/auth/calendar",
            "expires_at": datetime.now(UTC).isoformat(),
            "granted_at": datetime.now(UTC).isoformat(),
        })

        assert db.runtime_get("auth.google.scopes") is None
        assert db.runtime_get("auth.google.scopes_checked_at") is None


# ===========================================================================
# scripts/auth.py — _google_status granted_at and scope note
# ===========================================================================

class TestGoogleStatusOutput:
    def test_status_uses_granted_at_not_expires_at(self, capsys):
        """_google_status() must display granted_at (distinct from expires_at)
        and must not report expires_at as the grant timestamp."""
        import json as _json

        from auth import store as store_mod
        from auth.google import write_grant_to_keychain
        from scripts.auth import _google_status

        store_mod._reset_store()
        store_mod._store = store_mod.MemoryStore()

        grant_ts = "2026-01-15T10:00:00+00:00"
        exp_ts = "2026-01-15T11:00:00+00:00"
        write_grant_to_keychain({
            "client_id": "cid",
            "client_secret": "csec",
            "refresh_token": "rtok",
            "access_token": "atok",
            "scope": "https://www.googleapis.com/auth/calendar",
            "expires_at": exp_ts,
            "granted_at": grant_ts,
        })

        rc = _google_status()
        assert rc == 0
        out = capsys.readouterr().out
        data = _json.loads(out)
        assert data["granted_at"] == grant_ts
        assert data["expires_at"] == exp_ts
        # granted_at and expires_at must be distinct fields
        assert "scopes_requested_at_grant" in data

    def test_status_without_granted_at_shows_unknown(self, capsys):
        """Old grant blobs lacking 'granted_at' must display 'unknown', not
        the expires_at value (previous control-plane lie)."""
        import json as _json

        from auth import store as store_mod
        from auth.google import _GRANT_KEY
        from scripts.auth import _google_status

        store_mod._reset_store()
        mem = store_mod.MemoryStore()
        store_mod._store = mem

        import json as _j
        # Manually write a legacy grant blob without 'granted_at'.
        legacy = {
            "client_id": "cid",
            "client_secret": "csec",
            "refresh_token": "rtok",
            "access_token": "atok",
            "scope": "https://www.googleapis.com/auth/calendar",
            "expires_at": "2026-01-15T11:00:00+00:00",
        }
        mem.set("google", _GRANT_KEY, _j.dumps(legacy))

        rc = _google_status()
        assert rc == 0
        out = capsys.readouterr().out
        data = _json.loads(out)
        assert data["granted_at"] == "unknown"


# ===========================================================================
# Hook _precheck_scopes
# ===========================================================================

class TestPrecheckScopes:
    """Tests for agents.hooks._precheck_scopes in shadow / enforce / off modes."""

    def _patch_scope_config_and_provider(self, monkeypatch, missing_scopes: list[str]):
        """Patch load_scope_config and get_provider so GoogleProvider returns
        a controlled set of scopes."""
        from auth.providers import ScopeConfig, ToolSpec

        cfg = ScopeConfig(
            tool_specs={
                "mcp__google_workspace__gmail_bulk_delete_messages": ToolSpec(
                    provider="google",
                    required_scopes=["https://mail.google.com/"],
                    action="nuke them",
                )
            },
            provider_templates={
                "google": (
                    "can't {action} — my {provider} grant doesn't cover "
                    "{missing_scopes}. run the grant command."
                )
            },
        )

        granted = {"https://www.googleapis.com/auth/gmail.modify"}

        class _FakeProvider:
            async def current_scopes(self):
                return granted

        import auth.providers as prov_mod
        monkeypatch.setattr(prov_mod, "load_scope_config", lambda: cfg)
        monkeypatch.setattr(prov_mod, "get_provider", lambda name: _FakeProvider())

    @pytest.mark.asyncio
    async def test_shadow_mode_logs_and_returns_none(self, monkeypatch, caplog):
        import logging
        self._patch_scope_config_and_provider(monkeypatch, missing_scopes=["https://mail.google.com/"])
        monkeypatch.setenv("AUTH_PRECHECK", "shadow")

        import importlib

        from agents import hooks
        importlib.reload(hooks)

        with caplog.at_level(logging.WARNING, logger="agents.hooks"):
            result = await hooks._precheck_scopes(
                "mcp__google_workspace__gmail_bulk_delete_messages", {}
            )

        assert result is None
        assert "scope_precheck shadow miss" in caplog.text

    @pytest.mark.asyncio
    async def test_enforce_mode_returns_deny_dict(self, monkeypatch):
        self._patch_scope_config_and_provider(monkeypatch, missing_scopes=["https://mail.google.com/"])
        monkeypatch.setenv("AUTH_PRECHECK", "enforce")

        import importlib

        from agents import hooks
        importlib.reload(hooks)

        result = await hooks._precheck_scopes(
            "mcp__google_workspace__gmail_bulk_delete_messages", {}
        )

        assert result is not None
        hook_out = result["hookSpecificOutput"]
        assert hook_out["hookEventName"] == "PreToolUse"
        assert hook_out["permissionDecision"] == "deny"
        reason = hook_out["permissionDecisionReason"]
        assert "nuke them" in reason
        assert "google" in reason
        assert "https://mail.google.com/" in reason

    @pytest.mark.asyncio
    async def test_off_mode_returns_none(self, monkeypatch):
        self._patch_scope_config_and_provider(monkeypatch, missing_scopes=["https://mail.google.com/"])
        monkeypatch.setenv("AUTH_PRECHECK", "off")

        import importlib

        from agents import hooks
        importlib.reload(hooks)

        result = await hooks._precheck_scopes(
            "mcp__google_workspace__gmail_bulk_delete_messages", {}
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_scopes_satisfied(self, monkeypatch):
        """When the provider has the required scope, precheck returns None."""
        from auth.providers import ScopeConfig, ToolSpec

        cfg = ScopeConfig(
            tool_specs={
                "mcp__google_workspace__gmail_bulk_delete_messages": ToolSpec(
                    provider="google",
                    required_scopes=["https://mail.google.com/"],
                    action="nuke them",
                )
            },
            provider_templates={"google": "can't {action}"},
        )

        class _FullProvider:
            async def current_scopes(self):
                return {"https://mail.google.com/"}

        import auth.providers as prov_mod
        monkeypatch.setattr(prov_mod, "load_scope_config", lambda: cfg)
        monkeypatch.setattr(prov_mod, "get_provider", lambda name: _FullProvider())
        monkeypatch.setenv("AUTH_PRECHECK", "enforce")

        import importlib

        from agents import hooks
        importlib.reload(hooks)

        result = await hooks._precheck_scopes(
            "mcp__google_workspace__gmail_bulk_delete_messages", {}
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_unregistered_tool(self, monkeypatch):
        """Unknown tool names pass through (no spec → no precheck)."""
        import auth.providers as prov_mod
        from auth.providers import ScopeConfig
        monkeypatch.setattr(prov_mod, "load_scope_config", lambda: ScopeConfig())
        monkeypatch.setenv("AUTH_PRECHECK", "enforce")

        import importlib

        from agents import hooks
        importlib.reload(hooks)

        result = await hooks._precheck_scopes("mcp__some_unknown__tool", {})
        assert result is None
