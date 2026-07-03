"""Sprint 7F: OAuth token hashing tests.

Covers:
  1. oauth_token_create returns plaintext; validate by hash returns row
  2. Plaintext token (not hashed) stored in DB is rejected by validate
  3. Expired token is rejected
  4. oauth_token_revoke removes the hash; subsequent validate returns None
  5. Migration is idempotent via 7B ledger (registered in KNOWN_MIGRATIONS)
  6. AuthMiddleware uses the hashed path (mock request)
  7. _oauth2_token_validate still works for OAuth 2.1 access tokens
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "oauth_hash_test.db"


@pytest.fixture()
def isolated_db(tmp_db_path: Path):
    """Fresh DB with schema + migrations applied via storage.db."""
    import storage.db as db_mod
    with patch.dict(os.environ, {"HIKARI_DB_PATH": str(tmp_db_path)}):
        db_mod._DB_PATH = tmp_db_path
        db_mod._reset_schema_sentinel()
        conn = db_mod._get_pooled_conn()
        yield conn, db_mod
        conn.close()
        db_mod._reset_schema_sentinel()


# ---------------------------------------------------------------------------
# Test 1 — create + validate round trip
# ---------------------------------------------------------------------------

class TestOauthTokenHashCreate:
    def test_create_returns_plaintext_validate_returns_metadata(self, isolated_db):
        _, db = isolated_db
        token = db.oauth_token_create(owner="test-owner", scopes="read:memory")
        assert isinstance(token, str)
        assert len(token) > 20

        result = db.oauth_token_validate(token)
        assert result is not None
        assert result["owner"] == "test-owner"
        assert result["scopes"] == "read:memory"

    def test_hash_stored_not_plaintext(self, isolated_db):
        conn, db = isolated_db
        token = db.oauth_token_create(owner="test-owner")
        expected_hash = hashlib.sha256(token.encode()).hexdigest()

        row = conn.execute(
            "SELECT token_hash FROM oauth_token_hashes WHERE token_hash = ?",
            (expected_hash,),
        ).fetchone()
        assert row is not None
        # Plaintext token must NOT appear in the hash column
        plaintext_row = conn.execute(
            "SELECT token_hash FROM oauth_token_hashes WHERE token_hash = ?",
            (token,),
        ).fetchone()
        assert plaintext_row is None


# ---------------------------------------------------------------------------
# Test 2 — plaintext (unhashed) token stored directly is rejected
# ---------------------------------------------------------------------------

class TestOauthTokenHashRejectPlaintext:
    def test_direct_insert_of_plaintext_is_not_validated(self, isolated_db):
        conn, db = isolated_db
        # Insert a "token" directly (as if it were stored plaintext like legacy system)
        fake_plaintext = "definitelyNotHashedToken1234"
        conn.execute(
            "INSERT INTO oauth_token_hashes(token_hash, owner, created_at) "
            "VALUES (?, ?, datetime('now'))",
            (fake_plaintext, "attacker"),
        )
        conn.commit()
        # Calling validate with the same string: it hashes it, gets a different hash,
        # and the lookup returns nothing (sha256(fake_plaintext) != fake_plaintext).
        result = db.oauth_token_validate(fake_plaintext)
        assert result is None


# ---------------------------------------------------------------------------
# Test 3 — expired token is rejected
# ---------------------------------------------------------------------------

class TestOauthTokenHashExpiry:
    def test_expired_token_rejected(self, isolated_db):
        conn, db = isolated_db
        import hashlib
        from datetime import UTC, datetime, timedelta

        plaintext = "expiredTokenValue12345"
        token_hash = hashlib.sha256(plaintext.encode()).hexdigest()
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        conn.execute(
            "INSERT INTO oauth_token_hashes(token_hash, owner, created_at, expires_at) "
            "VALUES (?, ?, datetime('now'), ?)",
            (token_hash, "owner", past),
        )
        conn.commit()

        result = db.oauth_token_validate(plaintext)
        assert result is None


# ---------------------------------------------------------------------------
# Test 4 — revoke removes hash, subsequent validate returns None
# ---------------------------------------------------------------------------

class TestOauthTokenHashRevoke:
    def test_revoke_deletes_row(self, isolated_db):
        _, db = isolated_db
        token = db.oauth_token_create(owner="test-owner")
        assert db.oauth_token_validate(token) is not None

        revoked = db.oauth_token_revoke(token)
        assert revoked is True
        assert db.oauth_token_validate(token) is None

    def test_revoke_unknown_token_returns_false(self, isolated_db):
        _, db = isolated_db
        result = db.oauth_token_revoke("nonexistent_token_xyz")
        assert result is False


# ---------------------------------------------------------------------------
# Test 5 — migration is registered in KNOWN_MIGRATIONS + idempotent
# ---------------------------------------------------------------------------

class TestOauthTokenHashMigrationLedger:
    def test_migration_in_known_migrations(self):
        from storage.db import KNOWN_MIGRATIONS
        assert "migrate_oauth_tokens_to_hash" in KNOWN_MIGRATIONS

    def test_migration_registered_in_schema_migrations(self, isolated_db):
        conn, _ = isolated_db
        row = conn.execute(
            "SELECT name FROM schema_migrations WHERE name = ?",
            ("migrate_oauth_tokens_to_hash",),
        ).fetchone()
        # Should be stamped either as 'run' (ran the migration) or 'backfill'
        assert row is not None

    def test_migration_idempotent_reruns(self, isolated_db):
        conn, db = isolated_db
        from storage.db import _migrate_oauth_tokens_to_hash
        from storage.migrations import run_once
        # Re-running via run_once should return False (skipped — already ledgered).
        # Must pass the same tag the production call site uses, else the recorded
        # tag-checksum mismatches a freshly computed source-hash and reads as drift.
        result = run_once(conn, "migrate_oauth_tokens_to_hash",
                          _migrate_oauth_tokens_to_hash,
                          tag="migrate_oauth_tokens_to_hash")
        assert result is False

    def test_add_hash_columns_idempotent(self, isolated_db):
        conn, db = isolated_db
        from storage.db import _migrate_oauth_tokens_add_hash_columns
        from storage.migrations import run_once
        # Re-running via run_once should return False (skipped — already ledgered).
        # Pass the production tag so the recorded tag-checksum matches (see above).
        result = run_once(conn, "migrate_oauth_tokens_add_hash_columns",
                          _migrate_oauth_tokens_add_hash_columns,
                          tag="migrate_oauth_tokens_add_hash_columns")
        assert result is False

    def test_add_hash_columns_in_known_migrations(self):
        from storage.db import KNOWN_MIGRATIONS
        assert "migrate_oauth_tokens_add_hash_columns" in KNOWN_MIGRATIONS


# ---------------------------------------------------------------------------
# Test 6 — AuthMiddleware uses hashed validation path
# ---------------------------------------------------------------------------

class TestAuthMiddlewareHashedPath:
    def test_middleware_accepts_valid_hashed_token(self, isolated_db, tmp_db_path):
        _, db_mod = isolated_db
        token = db_mod.oauth_token_create(owner="test-cli")

        from mcp_external.launch import AuthMiddleware

        app_called = []

        async def _dummy_app(scope, receive, send):
            app_called.append(scope.get("state", {}))

        middleware = AuthMiddleware(_dummy_app)

        import asyncio

        async def run():
            scope = {
                "type": "http",
                "path": "/mcp",
                "headers": [
                    (b"authorization", f"Bearer {token}".encode()),
                ],
                "state": {},
                "server": ("127.0.0.1", 8765),
            }
            responses = []

            async def receive():
                return {}

            async def send(event):
                responses.append(event)

            with patch.dict(os.environ, {"HIKARI_DB_PATH": str(tmp_db_path),
                                          "HIKARI_MCP_SECRET": ""}):
                # Point db module at the test db
                with patch("mcp_external.launch.db", db_mod):
                    await middleware(scope, receive, send)

            return responses, app_called

        loop = asyncio.new_event_loop()
        try:
            responses, calls = loop.run_until_complete(run())
        finally:
            loop.close()
        # If app_called is non-empty, the request passed auth; if responses has a 401, it didn't.
        # The middleware calls the inner app OR sends 401.
        status_codes = [
            r.get("status") for r in responses if r.get("type") == "http.response.start"
        ]
        # No 401 should appear — the valid token should pass through.
        assert 401 not in status_codes


# ---------------------------------------------------------------------------
# Test 7 — _oauth2_token_validate still works for OAuth 2.1 access tokens
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Fix 5 — oauth_cleanup_expired sweeps oauth_token_hashes
# ---------------------------------------------------------------------------

class TestOauthCleanupExpiredHashedTokens:
    def test_cleanup_removes_expired_hashed_token(self, isolated_db):
        """oauth_cleanup_expired must delete rows from oauth_token_hashes where
        expires_at is in the past."""
        import hashlib
        from datetime import UTC, datetime, timedelta

        conn, db = isolated_db
        plaintext = "expiredHashedToken99"
        token_hash = hashlib.sha256(plaintext.encode()).hexdigest()
        past = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        conn.execute(
            "INSERT INTO oauth_token_hashes(token_hash, owner, created_at, expires_at) "
            "VALUES (?, ?, datetime('now'), ?)",
            (token_hash, "owner-cleanup", past),
        )
        conn.commit()

        # Confirm it's there before cleanup
        row = conn.execute(
            "SELECT token_hash FROM oauth_token_hashes WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        assert row is not None

        deleted = db.oauth_cleanup_expired()
        assert deleted >= 1

        row_after = conn.execute(
            "SELECT token_hash FROM oauth_token_hashes WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        assert row_after is None

    def test_cleanup_preserves_non_expired_hashed_token(self, isolated_db):
        """oauth_cleanup_expired must NOT delete hashed tokens that haven't expired."""
        conn, db = isolated_db
        token = db.oauth_token_create(owner="active-owner", scopes="read")
        import hashlib
        token_hash = hashlib.sha256(token.encode()).hexdigest()

        db.oauth_cleanup_expired()

        row = conn.execute(
            "SELECT token_hash FROM oauth_token_hashes WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        assert row is not None


class TestOauth2TokenValidate:
    def test_oauth2_validate_returns_row_for_access_token(self, isolated_db):
        conn, db = isolated_db
        # Insert a client and mint an access token
        import json
        conn.execute(
            "INSERT INTO oauth_clients(client_id, client_name, redirect_uris) "
            "VALUES ('cli1', 'test', ?)",
            (json.dumps(["http://localhost"]),),
        )
        conn.commit()

        access_token = db.oauth_token_mint("cli1", "access", ttl_seconds=3600)
        row = db._oauth2_token_validate(access_token)
        assert row is not None
        assert row["token_type"] == "access"
        assert row["client_id"] == "cli1"

    def test_oauth2_validate_returns_none_for_unknown_token(self, isolated_db):
        _, db = isolated_db
        assert db._oauth2_token_validate("not_a_real_token") is None
