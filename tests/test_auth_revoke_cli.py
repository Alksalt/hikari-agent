"""revoke() truthful-success tests.

Covers:
  1. GoogleProvider.revoke() / GitHubPATProvider.revoke() / NotionOAuthProvider.revoke()
     return True on success and False (with a WARNING log) when the underlying
     store operation raises.
  2. scripts.auth's _google_revoke / _github_revoke / _notion_revoke CLI handlers
     print the success line and return 0 only when revoke() returns True; on
     False they print an error and return a non-zero exit code.
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import scripts.auth as auth_cli
from auth.github import GitHubPATProvider
from auth.google import GoogleProvider
from auth.notion import NotionOAuthProvider
from auth.store import MemoryStore

# ---------------------------------------------------------------------------
# Provider.revoke() return-value semantics
# ---------------------------------------------------------------------------

class TestGoogleProviderRevoke:
    def test_returns_true_on_success(self):
        store = MemoryStore()
        store.set("google", "refresh_token", "rtok")
        provider = GoogleProvider(store)
        with patch("httpx.post"):
            assert provider.revoke() is True

    def test_returns_false_and_logs_warning_on_store_failure(self, caplog):
        store = MemoryStore()
        provider = GoogleProvider(store)
        with (
            patch("httpx.post"),
            patch.object(store, "clear", side_effect=RuntimeError("keychain locked")),
            caplog.at_level(logging.WARNING, logger="auth.google"),
        ):
            assert provider.revoke() is False
        assert any("store clear failed" in r.message for r in caplog.records)


class TestGitHubPATProviderRevoke:
    def test_returns_true_on_success(self):
        store = MemoryStore()
        provider = GitHubPATProvider(store)
        assert provider.revoke() is True

    def test_returns_false_and_logs_warning_on_store_failure(self, caplog):
        store = MemoryStore()
        provider = GitHubPATProvider(store)
        with (
            patch.object(store, "clear", side_effect=RuntimeError("keychain locked")),
            caplog.at_level(logging.WARNING, logger="auth.github"),
        ):
            assert provider.revoke() is False
        assert any("GitHubPATProvider.revoke" in r.message for r in caplog.records)


class TestNotionOAuthProviderRevoke:
    def test_returns_true_on_success(self):
        provider = NotionOAuthProvider()
        with patch("auth.notion.default_store", return_value=MemoryStore()):
            assert provider.revoke() is True

    def test_returns_false_and_logs_warning_on_clear_failure(self, caplog):
        provider = NotionOAuthProvider()
        store = MemoryStore()
        with (
            patch("auth.notion.default_store", return_value=store),
            patch.object(store, "clear", side_effect=RuntimeError("keychain locked")),
            caplog.at_level(logging.WARNING, logger="auth.notion"),
        ):
            assert provider.revoke() is False
        assert any("NotionOAuthProvider.revoke" in r.message for r in caplog.records)

    def test_returns_false_and_logs_warning_on_set_failure(self, caplog):
        provider = NotionOAuthProvider()
        store = MemoryStore()
        with (
            patch("auth.notion.default_store", return_value=store),
            patch.object(store, "set", side_effect=RuntimeError("keychain locked")),
            caplog.at_level(logging.WARNING, logger="auth.notion"),
        ):
            assert provider.revoke() is False
        assert any("clearing" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# scripts.auth CLI handlers only print success / exit 0 on True
# ---------------------------------------------------------------------------

class TestGoogleRevokeCli:
    def test_prints_success_and_returns_0_on_true(self, capsys):
        fake_provider = MagicMock()
        fake_provider.revoke.return_value = True
        with patch("auth.google.GoogleProvider", return_value=fake_provider):
            rc = auth_cli._google_revoke()
        assert rc == 0
        assert "deleted" in capsys.readouterr().out

    def test_prints_error_and_returns_nonzero_on_false(self, capsys):
        fake_provider = MagicMock()
        fake_provider.revoke.return_value = False
        with patch("auth.google.GoogleProvider", return_value=fake_provider):
            rc = auth_cli._google_revoke()
        assert rc != 0
        assert "failed" in capsys.readouterr().err


class TestGithubRevokeCli:
    def test_prints_success_and_returns_0_on_true(self, capsys):
        fake_provider = MagicMock()
        fake_provider.revoke.return_value = True
        with patch("auth.github.GitHubPATProvider", return_value=fake_provider):
            rc = auth_cli._github_revoke()
        assert rc == 0
        assert "deleted" in capsys.readouterr().out

    def test_prints_error_and_returns_nonzero_on_false(self, capsys):
        fake_provider = MagicMock()
        fake_provider.revoke.return_value = False
        with patch("auth.github.GitHubPATProvider", return_value=fake_provider):
            rc = auth_cli._github_revoke()
        assert rc != 0
        assert "failed" in capsys.readouterr().err


class TestNotionRevokeCli:
    def test_prints_success_and_returns_0_on_true(self, capsys):
        fake_provider = MagicMock()
        fake_provider.revoke.return_value = True
        with patch("auth.notion.NotionOAuthProvider", return_value=fake_provider):
            rc = auth_cli._notion_revoke()
        assert rc == 0
        assert "deleted" in capsys.readouterr().out

    def test_prints_error_and_returns_nonzero_on_false(self, capsys):
        fake_provider = MagicMock()
        fake_provider.revoke.return_value = False
        with patch("auth.notion.NotionOAuthProvider", return_value=fake_provider):
            rc = auth_cli._notion_revoke()
        assert rc != 0
        assert "failed" in capsys.readouterr().err
