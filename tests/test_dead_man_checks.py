"""Sprint 7F: dead-man monitor tests.

Covers:
  1. check_agent_running returns True/False based on launchctl output
  2. check_db_mtime_fresh returns True when file is recent, False when old/missing
  3. check_backup_fresh returns True for recent .tar.age, False for old/missing
  4. check_mcp_external returns True on 200, False on error
  5. check_cloudflared_running returns True/False based on launchctl output
  6. All-fail produces ONE Telegram post containing all failed check names
  7. Network down (httpx raises) doesn't crash the loop
  8. Missing env vars degrade to stderr log only (no crash)
  9. dry-run prints status and returns 0
 10. check_backup_fresh falls back to legacy .db files if no .tar.age found
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------
import scripts.dead_man as dm

# ---------------------------------------------------------------------------
# Test 1 — check_agent_running
# ---------------------------------------------------------------------------

class TestCheckAgentRunning:
    def test_returns_true_when_pid_in_print_output(self):
        output = "pid = 12345\nlast exit code = 0\n"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=output)
            assert dm.check_agent_running() is True

    def test_returns_false_when_pid_zero_or_absent(self):
        output = "pid = 0\nlast exit code = 1\n"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=output)
            assert dm.check_agent_running() is False

    def test_returns_false_when_launchctl_returns_nonzero(self):
        # returncode != 0 means service not found in both domains.
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=113, stdout="")
            assert dm.check_agent_running() is False

    def test_returns_false_on_subprocess_exception(self):
        with patch("subprocess.run", side_effect=OSError("not found")):
            assert dm.check_agent_running() is False


# ---------------------------------------------------------------------------
# Test 2 — check_db_mtime_fresh
# ---------------------------------------------------------------------------

class TestCheckDbMtimeFresh:
    def test_returns_true_for_recent_file(self, tmp_path):
        db_file = tmp_path / "hikari.db"
        db_file.write_bytes(b"x")
        with patch.object(dm, "DB_PATH", db_file):
            assert dm.check_db_mtime_fresh() is True

    def test_returns_false_for_old_file(self, tmp_path):
        db_file = tmp_path / "hikari.db"
        db_file.write_bytes(b"x")
        # Backdating mtime by 31 min
        old_time = time.time() - (31 * 60)
        import os
        os.utime(db_file, (old_time, old_time))
        with patch.object(dm, "DB_PATH", db_file):
            assert dm.check_db_mtime_fresh() is False

    def test_returns_false_when_db_missing(self, tmp_path):
        with patch.object(dm, "DB_PATH", tmp_path / "missing.db"):
            assert dm.check_db_mtime_fresh() is False


# ---------------------------------------------------------------------------
# Test 3 — check_backup_fresh
# ---------------------------------------------------------------------------

class TestCheckBackupFresh:
    def test_returns_true_for_recent_tar_age(self, tmp_path):
        backup = tmp_path / "hikari-20260101.tar.age"
        backup.write_bytes(b"enc")
        with patch.object(dm, "BACKUP_DIR", tmp_path):
            assert dm.check_backup_fresh() is True

    def test_returns_false_for_old_tar_age(self, tmp_path):
        backup = tmp_path / "hikari-20200101.tar.age"
        backup.write_bytes(b"enc")
        old_time = time.time() - (31 * 3600)
        import os
        os.utime(backup, (old_time, old_time))
        with patch.object(dm, "BACKUP_DIR", tmp_path):
            assert dm.check_backup_fresh() is False

    def test_returns_false_when_no_backups(self, tmp_path):
        with patch.object(dm, "BACKUP_DIR", tmp_path):
            assert dm.check_backup_fresh() is False

    def test_falls_back_to_legacy_db_files(self, tmp_path):
        # No .tar.age but a recent .db file
        backup = tmp_path / "hikari-20260101.db"
        backup.write_bytes(b"db")
        with patch.object(dm, "BACKUP_DIR", tmp_path):
            assert dm.check_backup_fresh() is True

    def test_returns_false_when_backup_dir_missing(self, tmp_path):
        missing = tmp_path / "nonexistent_backups"
        with patch.object(dm, "BACKUP_DIR", missing):
            assert dm.check_backup_fresh() is False


# ---------------------------------------------------------------------------
# Test 4 — check_mcp_external
# ---------------------------------------------------------------------------

class TestCheckMcpExternal:
    def test_returns_true_on_200(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        with patch.object(dm, "_HAS_MCP_EXTERNAL", True):
            with patch("httpx.get", return_value=mock_response):
                assert dm.check_mcp_external() is True

    def test_returns_true_on_401(self):
        """401 means the server is up (auth required), not down."""
        mock_response = MagicMock()
        mock_response.status_code = 401
        with patch.object(dm, "_HAS_MCP_EXTERNAL", True):
            with patch("httpx.get", return_value=mock_response):
                assert dm.check_mcp_external() is True

    def test_returns_true_on_405(self):
        mock_response = MagicMock()
        mock_response.status_code = 405
        with patch.object(dm, "_HAS_MCP_EXTERNAL", True):
            with patch("httpx.get", return_value=mock_response):
                assert dm.check_mcp_external() is True

    def test_returns_false_on_500(self):
        mock_response = MagicMock()
        mock_response.status_code = 500
        with patch.object(dm, "_HAS_MCP_EXTERNAL", True):
            with patch("httpx.get", return_value=mock_response):
                assert dm.check_mcp_external() is False

    def test_returns_false_on_connection_error(self):
        import httpx
        with patch.object(dm, "_HAS_MCP_EXTERNAL", True):
            with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
                assert dm.check_mcp_external() is False

    def test_returns_false_on_timeout(self):
        import httpx
        with patch.object(dm, "_HAS_MCP_EXTERNAL", True):
            with patch("httpx.get", side_effect=httpx.TimeoutException("timeout")):
                assert dm.check_mcp_external() is False

    def test_returns_true_when_mcp_flag_off(self):
        """Skipped (returns True) when HIKARI_HAS_MCP_EXTERNAL flag not set."""
        with patch.object(dm, "_HAS_MCP_EXTERNAL", False):
            assert dm.check_mcp_external() is True


# ---------------------------------------------------------------------------
# Test 5 — check_cloudflared_running
# ---------------------------------------------------------------------------

class TestCheckCloudflaredRunning:
    def test_returns_true_when_pid_present(self):
        output = "pid = 7\nlast exit code = 0\n"
        with patch.object(dm, "_HAS_MCP_EXTERNAL", True):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout=output)
                assert dm.check_cloudflared_running() is True

    def test_returns_false_when_pid_zero_or_absent(self):
        output = "pid = 0\nlast exit code = 1\n"
        with patch.object(dm, "_HAS_MCP_EXTERNAL", True):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout=output)
                assert dm.check_cloudflared_running() is False

    def test_returns_true_when_flag_off(self):
        """Skipped when HIKARI_HAS_MCP_EXTERNAL != '1'."""
        with patch.object(dm, "_HAS_MCP_EXTERNAL", False):
            assert dm.check_cloudflared_running() is True


# ---------------------------------------------------------------------------
# Test 6 — all-fail produces ONE Telegram post with all check names
# ---------------------------------------------------------------------------

class TestPostAlert:
    def test_all_fail_sends_one_telegram_post(self):
        with (
            patch("httpx.post") as mock_post,
            patch.object(dm, "DEADMAN_TOKEN", "bot123"),
            patch.object(dm, "OWNER_ID", "456"),
            patch.object(dm, "_telegram_probe_ok", return_value=True),
        ):
            dm.post_alert(["agent", "db_fresh", "backup_fresh"])
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        body = call_kwargs[1]["json"] if call_kwargs[1] else call_kwargs[0][1]
        msg = body["text"]
        assert "agent" in msg
        assert "db_fresh" in msg
        assert "backup_fresh" in msg

    def test_single_call_regardless_of_fail_count(self):
        checks = ["agent", "db_fresh", "backup_fresh", "mcp_external", "cloudflared"]
        with (
            patch("httpx.post") as mock_post,
            patch.object(dm, "DEADMAN_TOKEN", "bot123"),
            patch.object(dm, "OWNER_ID", "456"),
            patch.object(dm, "_telegram_probe_ok", return_value=True),
        ):
            dm.post_alert(checks)
        assert mock_post.call_count == 1


# ---------------------------------------------------------------------------
# Test 7 — network down doesn't crash the loop
# ---------------------------------------------------------------------------

class TestNetworkDown:
    def test_network_down_main_returns_0(self, tmp_path):
        """If mcp_external check raises, main loop catches and continues."""
        import httpx

        def _raise(*a, **kw):
            raise httpx.ConnectError("network down")

        with (
            patch.object(dm, "check_agent_running", return_value=True),
            patch.object(dm, "check_db_mtime_fresh", return_value=True),
            patch.object(dm, "check_backup_fresh", return_value=True),
            patch("httpx.get", side_effect=_raise),
            patch("httpx.head", side_effect=httpx.ConnectError("network down")),
            patch.object(dm, "check_cloudflared_running", return_value=True),
            patch("httpx.post"),  # suppress alert post
            patch.object(dm, "DEADMAN_TOKEN", "bot123"),
            patch.object(dm, "OWNER_ID", "456"),
            patch.object(dm, "_STRIKE_FILE", tmp_path / "deadman_strikes.txt"),
        ):
            # Call main() directly via argparse-bypassing approach
            with patch("sys.argv", ["dead_man.py"]):
                rc = dm.main()
            assert rc == 0


# ---------------------------------------------------------------------------
# Test 8 — missing env vars degrade to stderr log
# ---------------------------------------------------------------------------

class TestMissingEnvVars:
    def test_no_token_writes_to_stderr_not_raises(self, capsys):
        with patch.object(dm, "DEADMAN_TOKEN", None):
            with patch.object(dm, "OWNER_ID", None):
                dm.post_alert(["agent"])
        captured = capsys.readouterr()
        assert "agent" in captured.err
        # Should NOT have raised

    def test_empty_token_writes_to_stderr(self, capsys):
        with patch.object(dm, "DEADMAN_TOKEN", ""):
            with patch.object(dm, "OWNER_ID", "123"):
                dm.post_alert(["db_fresh"])
        captured = capsys.readouterr()
        assert "db_fresh" in captured.err


# ---------------------------------------------------------------------------
# Test 9 — dry-run prints status and returns 0
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_returns_0_and_prints(self, capsys, tmp_path):
        with (
            patch.object(dm, "check_agent_running", return_value=True),
            patch.object(dm, "check_db_mtime_fresh", return_value=False),
            patch.object(dm, "check_backup_fresh", return_value=True),
            patch.object(dm, "check_mcp_external", return_value=True),
            patch.object(dm, "check_cloudflared_running", return_value=False),
            patch("sys.argv", ["dead_man.py", "--dry-run"]),
        ):
            rc = dm.main()
        assert rc == 0
        captured = capsys.readouterr()
        assert "db_fresh" in captured.out
        assert "cloudflared" in captured.out
        assert "FAIL" in captured.out
        assert "OK" in captured.out
