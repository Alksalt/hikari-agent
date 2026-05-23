"""Phase G OAuth tests.

Covers:
  - Google keychain write/read round-trip
  - Google runtime spawn env injection (keychain token, not .env)
  - Notion PKCE challenge generation (S256)
  - Notion DCR /register flow (mocked HTTP)
  - Notion token persisted as access_token (no refresh/mutex)
  - GitHub PAT classic scope detection (X-OAuth-Scopes present)
  - GitHub PAT fine-grained detection (X-OAuth-Scopes absent)
  - Scope precheck enforce mode denies missing scope
  - AUTH_PRECHECK_OVERRIDE=shadow overrides config enforce
"""
from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auth.store import MemoryStore


# ---------------------------------------------------------------------------
# Shared store fixture — one MemoryStore instance per test, injected into
# all auth modules via monkeypatching their imported `default_store`.
# ---------------------------------------------------------------------------

@pytest.fixture()
def mem_store():
    """Return a fresh MemoryStore for use in tests."""
    return MemoryStore()


@pytest.fixture(autouse=True)
def _patch_default_store(mem_store, monkeypatch, tmp_path):
    """Patch default_store everywhere so no OS keychain is touched."""
    _ds = lambda: mem_store  # noqa: E731

    import auth.store as store_mod
    import auth.google as google_mod
    import auth.notion as notion_mod
    import auth.github as github_mod

    monkeypatch.setattr(store_mod, "default_store", _ds)
    monkeypatch.setattr(google_mod, "default_store", _ds)
    monkeypatch.setattr(notion_mod, "default_store", _ds)
    monkeypatch.setattr(github_mod, "default_store", _ds)

    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from storage import db
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()

    # Reset auth provider singletons.
    import auth.providers as prov_mod
    prov_mod._reset_providers()
    prov_mod._scope_config = None

    from agents import config as agent_config
    agent_config.reload()

    yield

    prov_mod._reset_providers()
    prov_mod._scope_config = None


# ===========================================================================
# Step 1 — Google keychain round-trip
# ===========================================================================

class TestGoogleKeychainRoundTrip:
    def test_write_then_read_returns_same_payload(self, mem_store):
        from auth.google import write_grant_to_keychain, read_grant_from_keychain

        payload = {
            "client_id": "cid123",
            "client_secret": "csec456",
            "access_token": "atok",
            "refresh_token": "rtok",
            "scope": "https://mail.google.com/ https://www.googleapis.com/auth/calendar",
            "expires_at": "2026-05-23T10:00:00+00:00",
        }
        write_grant_to_keychain(payload)
        result = read_grant_from_keychain()

        assert result is not None
        assert result["client_id"] == "cid123"
        assert result["refresh_token"] == "rtok"
        assert "https://mail.google.com/" in result["scope"]

    def test_read_returns_none_when_empty(self, mem_store):
        from auth.google import read_grant_from_keychain
        result = read_grant_from_keychain()
        assert result is None

    def test_individual_creds_also_written(self, mem_store):
        from auth.google import write_grant_to_keychain

        payload = {
            "client_id": "cid",
            "client_secret": "csec",
            "refresh_token": "rtok",
            "scope": "",
            "expires_at": "2026-01-01T00:00:00+00:00",
        }
        write_grant_to_keychain(payload)
        assert mem_store.get("google", "client_id") == "cid"
        assert mem_store.get("google", "client_secret") == "csec"
        assert mem_store.get("google", "refresh_token") == "rtok"


# ===========================================================================
# Step 2 — Google runtime spawn: env injected from keychain, not .env
# ===========================================================================

class TestGoogleRuntimeSpawnUsesKeychainToken:
    def test_keychain_token_injected_to_env(self, monkeypatch, mem_store):
        monkeypatch.delenv("GOOGLE_WORKSPACE_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_WORKSPACE_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("GOOGLE_WORKSPACE_REFRESH_TOKEN", raising=False)

        from auth.google import write_grant_to_keychain
        write_grant_to_keychain({
            "client_id": "kc-cid",
            "client_secret": "kc-csec",
            "refresh_token": "kc-rtok",
            "scope": "https://mail.google.com/",
            "expires_at": "2026-05-23T00:00:00+00:00",
        })

        import os
        import agents.runtime as runtime_mod
        runtime_mod._inject_keychain_tokens_to_env()

        assert os.environ.get("GOOGLE_WORKSPACE_CLIENT_ID") == "kc-cid"
        assert os.environ.get("GOOGLE_WORKSPACE_CLIENT_SECRET") == "kc-csec"
        assert os.environ.get("GOOGLE_WORKSPACE_REFRESH_TOKEN") == "kc-rtok"

    def test_existing_env_var_not_overwritten(self, monkeypatch, mem_store):
        monkeypatch.setenv("GOOGLE_WORKSPACE_REFRESH_TOKEN", "env-rtok")
        monkeypatch.delenv("GOOGLE_WORKSPACE_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_WORKSPACE_CLIENT_SECRET", raising=False)

        from auth.google import write_grant_to_keychain
        write_grant_to_keychain({
            "client_id": "kc-cid",
            "client_secret": "kc-csec",
            "refresh_token": "kc-rtok",
            "scope": "",
            "expires_at": "2026-05-23T00:00:00+00:00",
        })

        import os
        import agents.runtime as runtime_mod
        runtime_mod._inject_keychain_tokens_to_env()

        # The .env var must win over keychain.
        assert os.environ.get("GOOGLE_WORKSPACE_REFRESH_TOKEN") == "env-rtok"


# ===========================================================================
# Step 3 — Notion PKCE challenge round-trip
# ===========================================================================

class TestNotionPKCEChallenge:
    def test_challenge_is_s256_of_verifier(self):
        import base64
        import hashlib
        from auth.notion import generate_pkce_pair

        verifier, challenge = generate_pkce_pair()

        # Recompute expected S256 challenge.
        digest = hashlib.sha256(verifier.encode()).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        assert challenge == expected

    def test_verifier_is_url_safe(self):
        from auth.notion import generate_pkce_pair
        verifier, _ = generate_pkce_pair()
        import re
        assert re.fullmatch(r"[A-Za-z0-9_\-]+", verifier)

    def test_different_calls_produce_different_pairs(self):
        from auth.notion import generate_pkce_pair
        v1, c1 = generate_pkce_pair()
        v2, c2 = generate_pkce_pair()
        assert v1 != v2
        assert c1 != c2


# ===========================================================================
# Step 4 — Notion DCR register flow
# ===========================================================================

class TestNotionDCRRegisterFlow:
    def test_dcr_register_persists_client_to_keychain(self, monkeypatch, mem_store):
        import auth.notion as notion_mod
        import httpx

        fake_response_data = {
            "client_id": "notion-cid-abc",
            "client_secret": "notion-csec-xyz",
        }

        class _FakeResp:
            def raise_for_status(self): pass
            def json(self): return fake_response_data

        monkeypatch.setattr(httpx, "post", lambda *a, **kw: _FakeResp())

        result = notion_mod.dcr_register()
        assert result["client_id"] == "notion-cid-abc"

        raw = mem_store.get("notion", "client")
        assert raw is not None
        blob = json.loads(raw)
        assert blob["client_id"] == "notion-cid-abc"
        assert blob["client_secret"] == "notion-csec-xyz"


# ===========================================================================
# Step 5 — Notion token persisted as access_token (no refresh/mutex)
# ===========================================================================

class TestNotionAccessTokenPersistence:
    @pytest.mark.asyncio
    async def test_current_scopes_present_when_access_token_in_keychain(self, mem_store):
        import json
        import auth.notion as notion_mod

        token_blob = {
            "access_token": "ntn_long_lived_token",
            "workspace_id": "ws1",
            "scopes": "*",
            "issued_at": "2026-05-23T00:00:00+00:00",
        }
        mem_store.set("notion", "token", json.dumps(token_blob))

        provider = notion_mod.NotionOAuthProvider(store=mem_store)
        scopes = await provider.current_scopes()
        assert "_present" in scopes

    @pytest.mark.asyncio
    async def test_current_scopes_empty_when_no_token(self, mem_store, monkeypatch):
        import auth.notion as notion_mod
        monkeypatch.delenv("NOTION_TOKEN", raising=False)

        provider = notion_mod.NotionOAuthProvider(store=mem_store)
        scopes = await provider.current_scopes()
        assert scopes == set()

    @pytest.mark.asyncio
    async def test_refresh_is_noop_returns_stored_token(self, mem_store):
        import json
        import auth.notion as notion_mod

        token_blob = {
            "access_token": "ntn_long_lived_token",
            "workspace_id": "ws1",
            "scopes": "*",
            "issued_at": "2026-05-23T00:00:00+00:00",
        }
        mem_store.set("notion", "token", json.dumps(token_blob))

        provider = notion_mod.NotionOAuthProvider(store=mem_store)
        result = await provider.refresh()
        assert result == "ntn_long_lived_token"


# ===========================================================================
# Step 6 — GitHub classic PAT scope detection
# ===========================================================================

class TestGitHubClassicPAT:
    def test_classic_pat_detects_scopes_from_header(self, monkeypatch, mem_store):
        import httpx
        from auth.github import paste_and_persist

        class _FakeResp:
            status_code = 200
            headers = {"X-OAuth-Scopes": "repo, workflow, read:org"}
            def raise_for_status(self): pass
            def json(self): return {"login": "testuser"}

        monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResp())

        blob = paste_and_persist("ghp_faketoken")
        assert blob["kind"] == "classic"
        assert "repo" in blob["scopes"]
        assert "workflow" in blob["scopes"]
        assert "read:org" in blob["scopes"]
        assert blob["login"] == "testuser"

    def test_classic_pat_persisted_to_keychain(self, monkeypatch, mem_store):
        import httpx
        from auth.github import paste_and_persist

        class _FakeResp:
            headers = {"X-OAuth-Scopes": "repo"}
            def raise_for_status(self): pass
            def json(self): return {"login": "u"}

        monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResp())
        paste_and_persist("ghp_tok")

        raw = mem_store.get("github", "token")
        assert raw is not None
        stored = json.loads(raw)
        assert stored["kind"] == "classic"
        assert stored["token"] == "ghp_tok"


# ===========================================================================
# Step 7 — GitHub fine-grained PAT wildcard
# ===========================================================================

class TestGitHubFineGrainedPAT:
    def test_fine_grained_pat_marks_wildcard_when_no_header(self, monkeypatch, mem_store):
        import httpx
        from auth.github import paste_and_persist

        class _FakeResp:
            headers = {}  # no X-OAuth-Scopes
            def raise_for_status(self): pass
            def json(self): return {"login": "me"}

        monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResp())

        blob = paste_and_persist("github_pat_finegrained")
        assert blob["kind"] == "fine-grained"
        assert blob["scopes"] == ["*"]

    @pytest.mark.asyncio
    async def test_fine_grained_current_scopes_returns_wildcard(self, monkeypatch, mem_store):
        import httpx
        from auth.github import paste_and_persist, GitHubPATProvider

        class _FakeResp:
            headers = {}
            def raise_for_status(self): pass
            def json(self): return {"login": "me"}

        monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResp())
        paste_and_persist("github_pat_fg")

        provider = GitHubPATProvider(store=mem_store)
        scopes = await provider.current_scopes()
        assert "*" in scopes


# ===========================================================================
# Step 8 — Scope precheck enforce mode denies missing scope
# ===========================================================================

class TestScopePrecheckEnforce:
    def _patch_scope_config_and_provider(self, monkeypatch, granted_scopes: set[str]):
        from auth.providers import ToolSpec, ScopeConfig

        cfg_obj = ScopeConfig(
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
                    "{missing_scopes}. run `uv run python -m scripts.auth "
                    "{provider} grant --add {missing_scopes}` and re-launch me."
                )
            },
        )

        class _FakeProvider:
            async def current_scopes(self):
                return granted_scopes

        import auth.providers as prov_mod
        monkeypatch.setattr(prov_mod, "load_scope_config", lambda: cfg_obj)
        monkeypatch.setattr(prov_mod, "get_provider", lambda name: _FakeProvider())

    @pytest.mark.asyncio
    async def test_enforce_denies_missing_scope(self, monkeypatch):
        self._patch_scope_config_and_provider(
            monkeypatch,
            granted_scopes={"https://www.googleapis.com/auth/gmail.modify"},
        )
        # Ensure override env is absent, AUTH_PRECHECK env is absent.
        monkeypatch.delenv("AUTH_PRECHECK", raising=False)
        monkeypatch.delenv("AUTH_PRECHECK_OVERRIDE", raising=False)
        # Config says enforce via engagement.yaml (now set there).
        # Monkeypatch _load to inject auth.precheck=enforce.
        from agents import config as agent_config
        import agents.config as cfg_mod
        original_load = cfg_mod._load
        cfg_mod._load.cache_clear()
        def _patched_load():
            data = dict(original_load())
            data = {**data, "auth": {"precheck": "enforce"}}
            return data
        monkeypatch.setattr(cfg_mod, "_load", _patched_load)

        from agents import hooks
        importlib.reload(hooks)

        result = await hooks._precheck_scopes(
            "mcp__google_workspace__gmail_bulk_delete_messages", {}
        )

        assert result is not None
        hook_out = result["hookSpecificOutput"]
        assert hook_out["permissionDecision"] == "deny"
        reason = hook_out["permissionDecisionReason"]
        assert "nuke them" in reason
        assert "https://mail.google.com/" in reason
        assert "scripts.auth" in reason

    @pytest.mark.asyncio
    async def test_enforce_allows_when_scope_present(self, monkeypatch):
        self._patch_scope_config_and_provider(
            monkeypatch,
            granted_scopes={"https://mail.google.com/"},
        )
        monkeypatch.delenv("AUTH_PRECHECK", raising=False)
        monkeypatch.delenv("AUTH_PRECHECK_OVERRIDE", raising=False)
        monkeypatch.setenv("AUTH_PRECHECK", "enforce")

        from agents import hooks
        importlib.reload(hooks)

        result = await hooks._precheck_scopes(
            "mcp__google_workspace__gmail_bulk_delete_messages", {}
        )
        assert result is None


# ===========================================================================
# Step 9 — AUTH_PRECHECK_OVERRIDE=shadow overrides config enforce
# ===========================================================================

class TestAuthPrecheckOverrideShadow:
    @pytest.mark.asyncio
    async def test_override_shadow_env_disables_deny(self, monkeypatch, caplog):
        import logging
        from auth.providers import ToolSpec, ScopeConfig

        cfg_obj = ScopeConfig(
            tool_specs={
                "mcp__google_workspace__gmail_bulk_delete_messages": ToolSpec(
                    provider="google",
                    required_scopes=["https://mail.google.com/"],
                    action="nuke them",
                )
            },
            provider_templates={"google": "can't {action}"},
        )

        class _FakeProvider:
            async def current_scopes(self):
                return {"https://www.googleapis.com/auth/gmail.modify"}  # missing the required scope

        import auth.providers as prov_mod
        monkeypatch.setattr(prov_mod, "load_scope_config", lambda: cfg_obj)
        monkeypatch.setattr(prov_mod, "get_provider", lambda name: _FakeProvider())

        # override env=shadow wins over both env and config.
        monkeypatch.setenv("AUTH_PRECHECK_OVERRIDE", "shadow")
        monkeypatch.setenv("AUTH_PRECHECK", "enforce")  # would deny without override

        from agents import hooks
        importlib.reload(hooks)

        with caplog.at_level(logging.WARNING, logger="agents.hooks"):
            result = await hooks._precheck_scopes(
                "mcp__google_workspace__gmail_bulk_delete_messages", {}
            )

        # Shadow mode: returns None (no deny) but logs a warning.
        assert result is None
        assert "scope_precheck shadow miss" in caplog.text
