"""dead_man.py strengthened-check semantics.

  1. check_agent_running uses launchctl print PID parsing (not grep).
  2. check_db_mtime_fresh uses the cookie row (deadman_cookie table) and
     falls back to file mtime.
  3. mcp_external / cloudflared checks are skipped when
     HIKARI_HAS_MCP_EXTERNAL != '1'.
  4. Telegram HTTPS probe with 3-strike debounce suppresses alerts below
     3 consecutive failures.
  5. post_alert does NOT send when Telegram is unreachable (strike < 3).
  6. post_alert sends when strike reaches 3.
"""

from __future__ import annotations

import os
import time
from unittest.mock import MagicMock, patch

import scripts.dead_man as dm


# ---------------------------------------------------------------------------
# 1 — check_agent_running uses launchctl print PID parsing
# ---------------------------------------------------------------------------

class TestAgentRunningLaunchctlPrint:
    def test_returns_true_when_pid_present_in_print_output(self):
        print_output = (
            "{\n"
            "    active count = 1\n"
            "    path = /Library/LaunchAgents/com.hikari.agent.plist\n"
            "    state = running\n"
            "\n"
            "    program = /usr/bin/python3\n"
            "    arguments = {\n"
            "        /usr/bin/python3\n"
            "        -m\n"
            "        agents\n"
            "    }\n"
            "\n"
            "    pid = 12345\n"
            "    last exit code = 0\n"
            "}"
        )
        mock_result = MagicMock(returncode=0, stdout=print_output)
        with patch("subprocess.run", return_value=mock_result):
            assert dm.check_agent_running() is True

    def test_returns_false_when_pid_zero(self):
        print_output = "pid = 0\nlast exit code = 1\n"
        mock_result = MagicMock(returncode=0, stdout=print_output)
        with patch("subprocess.run", return_value=mock_result):
            assert dm.check_agent_running() is False

    def test_returns_false_when_launchctl_print_fails(self):
        with patch("subprocess.run", side_effect=OSError("no launchctl")):
            assert dm.check_agent_running() is False

    def test_returns_false_when_service_not_found(self):
        mock_result = MagicMock(returncode=113, stdout="")
        with patch("subprocess.run", return_value=mock_result):
            assert dm.check_agent_running() is False


# ---------------------------------------------------------------------------
# 2 — check_db_mtime_fresh: cookie row takes priority over file mtime
# ---------------------------------------------------------------------------

class TestDbMtimeFreshCookieRow:
    def test_returns_true_when_cookie_row_recent(self, tmp_path):
        db_file = tmp_path / "hikari.db"
        db_file.write_bytes(b"x")

        import sqlite3
        import datetime

        conn = sqlite3.connect(str(db_file))
        conn.execute("CREATE TABLE deadman_cookie (key TEXT PRIMARY KEY, ts TEXT)")
        recent_ts = datetime.datetime.now(datetime.UTC).isoformat()
        conn.execute("INSERT INTO deadman_cookie VALUES ('heartbeat', ?)", (recent_ts,))
        conn.commit()
        conn.close()

        with patch.object(dm, "DB_PATH", db_file):
            assert dm.check_db_mtime_fresh() is True

    def test_returns_false_when_cookie_row_old(self, tmp_path):
        db_file = tmp_path / "hikari.db"
        db_file.write_bytes(b"x")

        import sqlite3
        import datetime

        conn = sqlite3.connect(str(db_file))
        conn.execute("CREATE TABLE deadman_cookie (key TEXT PRIMARY KEY, ts TEXT)")
        old_ts = (datetime.datetime.now(datetime.UTC)
                  - datetime.timedelta(hours=1)).isoformat()
        conn.execute("INSERT INTO deadman_cookie VALUES ('heartbeat', ?)", (old_ts,))
        conn.commit()
        conn.close()

        with patch.object(dm, "DB_PATH", db_file):
            assert dm.check_db_mtime_fresh() is False

    def test_falls_back_to_mtime_when_no_cookie_table(self, tmp_path):
        db_file = tmp_path / "hikari.db"
        db_file.write_bytes(b"x")
        # No deadman_cookie table — fallback to file mtime.
        with patch.object(dm, "DB_PATH", db_file):
            assert dm.check_db_mtime_fresh() is True

    def test_file_mtime_fallback_returns_false_for_old_file(self, tmp_path):
        db_file = tmp_path / "hikari.db"
        db_file.write_bytes(b"x")
        old_time = time.time() - (31 * 60)
        os.utime(db_file, (old_time, old_time))

        with patch.object(dm, "DB_PATH", db_file):
            # No cookie table → fallback path; old file → False
            assert dm.check_db_mtime_fresh() is False


# ---------------------------------------------------------------------------
# 3 — mcp_external / cloudflared skipped when HIKARI_HAS_MCP_EXTERNAL != '1'
# ---------------------------------------------------------------------------

class TestMcpExternalGuard:
    def test_check_mcp_external_returns_true_when_flag_off(self):
        with patch.object(dm, "_HAS_MCP_EXTERNAL", False):
            with patch("httpx.get", side_effect=AssertionError("should not be called")):
                assert dm.check_mcp_external() is True

    def test_check_cloudflared_returns_true_when_flag_off(self):
        with patch.object(dm, "_HAS_MCP_EXTERNAL", False):
            with patch("subprocess.run", side_effect=AssertionError("should not be called")):
                assert dm.check_cloudflared_running() is True

    def test_check_mcp_external_probes_when_flag_on(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        with patch.object(dm, "_HAS_MCP_EXTERNAL", True):
            with patch("httpx.get", return_value=mock_resp):
                assert dm.check_mcp_external() is True

    def test_check_cloudflared_probes_launchctl_when_flag_on(self):
        print_output = "pid = 9999\nlast exit code = 0\n"
        mock_result = MagicMock(returncode=0, stdout=print_output)
        with patch.object(dm, "_HAS_MCP_EXTERNAL", True):
            with patch("subprocess.run", return_value=mock_result):
                assert dm.check_cloudflared_running() is True


# ---------------------------------------------------------------------------
# 4 — Telegram probe 3-strike debounce
# ---------------------------------------------------------------------------

class TestTelegramProbeDebounce:
    def test_reachable_resets_strike_counter(self, tmp_path):
        strike_file = tmp_path / "strikes.txt"
        strike_file.write_text("2")
        mock_resp = MagicMock(status_code=200)
        with (
            patch.object(dm, "_STRIKE_FILE", strike_file),
            patch("httpx.head", return_value=mock_resp),
        ):
            result = dm._telegram_probe_ok()
        assert result is True
        assert strike_file.read_text().strip() == "0"

    def test_unreachable_increments_strike_and_suppresses_below_3(self, tmp_path):
        strike_file = tmp_path / "strikes.txt"
        strike_file.write_text("1")
        with (
            patch.object(dm, "_STRIKE_FILE", strike_file),
            patch("httpx.head", side_effect=Exception("network down")),
        ):
            result = dm._telegram_probe_ok()
        assert result is True  # strike=2, below threshold → suppressed
        assert strike_file.read_text().strip() == "2"

    def test_unreachable_alerts_at_third_strike(self, tmp_path):
        strike_file = tmp_path / "strikes.txt"
        strike_file.write_text("2")
        with (
            patch.object(dm, "_STRIKE_FILE", strike_file),
            patch("httpx.head", side_effect=Exception("network down")),
        ):
            result = dm._telegram_probe_ok()
        assert result is False  # strike=3 → alert fires
        assert strike_file.read_text().strip() == "3"


# ---------------------------------------------------------------------------
# 5 & 6 — post_alert respects the 3-strike debounce
# ---------------------------------------------------------------------------

class TestPostAlertDebounce:
    def test_post_alert_skips_telegram_send_when_probe_suppresses(self, capsys):
        with (
            patch.object(dm, "DEADMAN_TOKEN", "bot123"),
            patch.object(dm, "OWNER_ID", "456"),
            patch.object(dm, "_telegram_probe_ok", return_value=False),
            patch("httpx.post") as mock_post,
        ):
            dm.post_alert(["agent"])
        mock_post.assert_not_called()
        captured = capsys.readouterr()
        assert "debounce" in captured.err or "unreachable" in captured.err

    def test_post_alert_sends_when_probe_ok(self):
        with (
            patch.object(dm, "DEADMAN_TOKEN", "bot123"),
            patch.object(dm, "OWNER_ID", "456"),
            patch.object(dm, "_telegram_probe_ok", return_value=True),
            patch("httpx.post") as mock_post,
        ):
            dm.post_alert(["agent"])
        mock_post.assert_called_once()
