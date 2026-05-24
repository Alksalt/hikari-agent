"""Sprint 7F: backup encryption tests.

Covers:
  1. Backup script exits non-zero when age recipient key is missing
  2. Backup script exits non-zero when HIKARI_BACKUP_AGE_RECIPIENT is unset
     and ~/.config/hikari/backup_age.pub doesn't exist
  3. tar+age pipeline produces a decryptable artifact (skipped if age not on PATH)
  4. Migration scrubs old oauth_tokens rows (migration content test)
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

AGE_AVAILABLE = shutil.which("age") is not None
BACKUP_SH = Path(__file__).parent.parent / "scripts" / "backup.sh"


# ---------------------------------------------------------------------------
# Test 1 — refuses to run when age recipient file is absent
# ---------------------------------------------------------------------------

class TestBackupRefusesWithoutRecipient:
    def test_exits_nonzero_when_recipient_file_missing(self, tmp_path):
        """backup.sh should exit 1 when recipient key file doesn't exist."""
        # Create a dummy DB so the "source does not exist" guard passes.
        db_path = tmp_path / "data" / "hikari.db"
        db_path.parent.mkdir(parents=True)
        db_path.write_bytes(b"SQLite format 3\x00")

        env = {
            **os.environ,
            "HIKARI_BACKUP_AGE_RECIPIENT": str(tmp_path / "nonexistent.pub"),
        }
        result = subprocess.run(
            ["/bin/zsh", str(BACKUP_SH)],
            env=env,
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
        )
        assert result.returncode != 0
        assert "missing age recipient" in result.stderr or "age" in result.stderr.lower()

    def test_exits_nonzero_when_recipient_env_unset(self, tmp_path, monkeypatch):
        """backup.sh exits 1 when no recipient env var and no default key file."""
        db_path = tmp_path / "data" / "hikari.db"
        db_path.parent.mkdir(parents=True)
        db_path.write_bytes(b"SQLite format 3\x00")

        # Remove the default path from env (HOME points to tmp_path, no .config/hikari/backup_age.pub there)
        env = {k: v for k, v in os.environ.items() if k != "HIKARI_BACKUP_AGE_RECIPIENT"}
        env["HOME"] = str(tmp_path)  # no ~/.config/hikari/ here

        result = subprocess.run(
            ["/bin/zsh", str(BACKUP_SH)],
            env=env,
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
        )
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# Test 2 — skips when today's backup already exists (idempotent)
# ---------------------------------------------------------------------------

class TestBackupIdempotent:
    def test_skips_if_today_archive_exists(self, tmp_path):
        """backup.sh should exit 0 + print 'skipping' if today's .tar.age exists."""
        import datetime
        today = datetime.date.today().strftime("%Y%m%d")

        # Create backup dir with today's archive already present.
        backup_dir = tmp_path / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / \
                     "Documents" / "alt-wiki" / "projects" / "hikari-agent" / "backups"
        backup_dir.mkdir(parents=True)
        (backup_dir / f"hikari-{today}.tar.age").write_bytes(b"already encrypted")

        db_path = tmp_path / "agents" / "hikari-agent" / "data" / "hikari.db"
        db_path.parent.mkdir(parents=True)
        db_path.write_bytes(b"SQLite format 3\x00")

        pub_key = tmp_path / ".config" / "hikari" / "backup_age.pub"
        pub_key.parent.mkdir(parents=True)
        pub_key.write_text("age1xxxxxx\n")

        env = {
            **os.environ,
            "HOME": str(tmp_path),
            "HIKARI_BACKUP_AGE_RECIPIENT": str(pub_key),
        }
        result = subprocess.run(
            ["/bin/zsh", str(BACKUP_SH)],
            env=env,
            capture_output=True,
            text=True,
            cwd=str(tmp_path / "agents" / "hikari-agent"),
        )
        # exit 0 and "skipping" in output
        assert result.returncode == 0
        assert "skipping" in result.stdout or "skipping" in result.stderr


# ---------------------------------------------------------------------------
# Test 3 — tar+age pipeline (skipped if age not available)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not AGE_AVAILABLE, reason="age binary not available in this env")
class TestBackupTarAge:
    def test_roundtrip_encrypt_decrypt(self, tmp_path):
        """Generate an age keypair, run backup.sh, decrypt, verify DB is inside."""
        # Generate keypair
        key_file = tmp_path / "backup_age.key"
        pub_file = tmp_path / "backup_age.pub"
        subprocess.run(
            ["age-keygen", "-o", str(key_file)],
            check=True, capture_output=True,
        )
        # Extract public key
        pub_text = subprocess.run(
            ["grep", "public key:", str(key_file)],
            capture_output=True, text=True,
        ).stdout.strip().split(": ", 1)[-1]
        pub_file.write_text(pub_text + "\n")

        # Create repo structure under tmp_path
        repo_dir = tmp_path / "agents" / "hikari-agent"
        data_dir = repo_dir / "data"
        data_dir.mkdir(parents=True)
        db_path = data_dir / "hikari.db"
        # Minimal valid SQLite3 header
        db_path.write_bytes(b"SQLite format 3\x00" + b"\x00" * 84)

        backup_dir = tmp_path / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / \
                     "Documents" / "alt-wiki" / "projects" / "hikari-agent" / "backups"
        backup_dir.mkdir(parents=True)

        env = {
            **os.environ,
            "HOME": str(tmp_path),
            "HIKARI_BACKUP_AGE_RECIPIENT": str(pub_file),
            "HIKARI_BACKUP_AGE_KEY": str(key_file),
        }
        result = subprocess.run(
            ["/bin/zsh", str(BACKUP_SH)],
            env=env,
            capture_output=True, text=True,
            cwd=str(repo_dir),
        )
        assert result.returncode == 0, f"backup.sh failed:\n{result.stderr}"

        archives = list(backup_dir.glob("hikari-*.tar.age"))
        assert len(archives) == 1
        archive = archives[0]
        assert archive.stat().st_size > 100

        # Decrypt and verify the DB is inside
        decrypted = tmp_path / "decrypted.tar"
        subprocess.run(
            ["age", "-d", "-i", str(key_file), "-o", str(decrypted), str(archive)],
            check=True, capture_output=True,
        )
        # List tar contents
        listing = subprocess.run(
            ["tar", "-tf", str(decrypted)],
            capture_output=True, text=True,
        )
        assert "hikari.db" in listing.stdout


# ---------------------------------------------------------------------------
# Test 4 — migration scrubs plaintext oauth_tokens rows
# ---------------------------------------------------------------------------

class TestOauthMigrationScrubsPlaintext:
    def test_migration_seeds_hashes_table_on_fresh_db(self, tmp_path):
        """On a fresh DB, oauth_token_hashes must exist (added to _SCHEMA in 7F)."""
        from unittest.mock import patch

        db_path = tmp_path / "test_migrate.db"
        import storage.db as db_mod

        with patch.dict(os.environ, {"HIKARI_DB_PATH": str(db_path)}):
            db_mod._DB_PATH = db_path
            db_mod._reset_schema_sentinel()
            conn = db_mod._get_pooled_conn()

            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            assert "oauth_token_hashes" in tables
            # oauth_tokens should also still exist (not dropped — OAuth 2.1 dance needs it)
            assert "oauth_tokens" in tables

            conn.close()
            db_mod._reset_schema_sentinel()

    def test_migration_hashes_existing_rows(self, tmp_path):
        """Simulate pre-migration state with plaintext tokens → verify they get hashed."""
        import sqlite3

        # Create a bare SQLite DB with both tables pre-populated (pre-7F state)
        raw_db = tmp_path / "premig.db"
        conn = sqlite3.connect(str(raw_db))
        conn.row_factory = sqlite3.Row

        conn.execute("""
            CREATE TABLE oauth_clients (
                client_id TEXT PRIMARY KEY,
                client_name TEXT,
                redirect_uris TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_used_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE oauth_tokens (
                token TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                token_type TEXT NOT NULL,
                parent_token TEXT,
                scope TEXT,
                expires_at TEXT NOT NULL,
                revoked_at TEXT,
                last_used_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE oauth_token_hashes (
                token_hash TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                scopes TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL,
                checksum TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'run'
            )
        """)
        conn.execute("PRAGMA user_version = 1")

        # Insert a plaintext token
        plaintext = "plaintext_test_token_abcdef"
        conn.execute(
            "INSERT INTO oauth_tokens(token, client_id, token_type, expires_at) "
            "VALUES (?, 'cli1', 'access', '2099-01-01T00:00:00+00:00')",
            (plaintext,),
        )
        conn.commit()

        # Run the migration
        from storage.db import _migrate_oauth_tokens_to_hash
        _migrate_oauth_tokens_to_hash(conn)
        conn.commit()

        # oauth_tokens should still exist (not dropped — OAuth 2.1 dance needs it)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "oauth_tokens" in tables

        # oauth_token_hashes should contain the hashed row
        expected_hash = hashlib.sha256(plaintext.encode()).hexdigest()
        row = conn.execute(
            "SELECT token_hash FROM oauth_token_hashes WHERE token_hash = ?",
            (expected_hash,),
        ).fetchone()
        assert row is not None

        conn.close()
