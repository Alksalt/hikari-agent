"""dead_man.py self-heal: main() must kickstart the agent when it's down.

restart_agent() was fully implemented (launchctl kickstart -k) but never wired
into main() — the monitor only alerted, it never relaunched the wedged/stopped
bot. Covers:
  1. main() calls restart_agent() when check_agent_running() is False.
  2. main() does NOT call restart_agent() when the agent check passes but
     other checks fail.
  3. The alert text reflects the kickstart outcome (rc=0 on success, rc=1 on
     failure) while still firing regardless of restart success.
  4. --dry-run never triggers a restart (diagnostic-only).
"""
from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import patch

import scripts.dead_man as dm


def _patch_checks(stack: ExitStack, *, agent_ok: bool, db_fresh: bool = True) -> None:
    stack.enter_context(patch.object(dm, "check_agent_running", return_value=agent_ok))
    stack.enter_context(patch.object(dm, "check_db_mtime_fresh", return_value=db_fresh))
    stack.enter_context(patch.object(dm, "check_backup_fresh", return_value=True))
    stack.enter_context(patch.object(dm, "check_mcp_external", return_value=True))
    stack.enter_context(patch.object(dm, "check_cloudflared_running", return_value=True))


class TestSelfHealOnAgentDown:
    def test_main_invokes_restart_agent_when_agent_check_fails(self):
        with ExitStack() as stack:
            _patch_checks(stack, agent_ok=False)
            mock_restart = stack.enter_context(
                patch.object(dm, "restart_agent", return_value=True)
            )
            mock_alert = stack.enter_context(patch.object(dm, "post_alert"))
            stack.enter_context(patch("sys.argv", ["dead_man.py"]))
            rc = dm.main()
        assert rc == 0
        mock_restart.assert_called_once()
        mock_alert.assert_called_once()

    def test_main_does_not_invoke_restart_agent_when_agent_check_passes(self):
        with ExitStack() as stack:
            _patch_checks(stack, agent_ok=True, db_fresh=False)
            mock_restart = stack.enter_context(patch.object(dm, "restart_agent"))
            mock_alert = stack.enter_context(patch.object(dm, "post_alert"))
            stack.enter_context(patch("sys.argv", ["dead_man.py"]))
            rc = dm.main()
        assert rc == 0
        mock_restart.assert_not_called()
        mock_alert.assert_called_once()

    def test_alert_includes_kickstart_success_outcome(self):
        with ExitStack() as stack:
            _patch_checks(stack, agent_ok=False)
            stack.enter_context(patch.object(dm, "restart_agent", return_value=True))
            mock_alert = stack.enter_context(patch.object(dm, "post_alert"))
            stack.enter_context(patch("sys.argv", ["dead_man.py"]))
            dm.main()
        failed_arg = mock_alert.call_args[0][0]
        assert any("kickstart rc=0" in name for name in failed_arg)

    def test_alert_includes_kickstart_failure_outcome(self):
        with ExitStack() as stack:
            _patch_checks(stack, agent_ok=False)
            stack.enter_context(patch.object(dm, "restart_agent", return_value=False))
            mock_alert = stack.enter_context(patch.object(dm, "post_alert"))
            stack.enter_context(patch("sys.argv", ["dead_man.py"]))
            dm.main()
        failed_arg = mock_alert.call_args[0][0]
        assert any("kickstart rc=1" in name for name in failed_arg)

    def test_alert_still_fires_even_when_restart_fails(self):
        """Owner must still learn the agent was down even if the kickstart itself failed."""
        with ExitStack() as stack:
            _patch_checks(stack, agent_ok=False)
            stack.enter_context(patch.object(dm, "restart_agent", return_value=False))
            mock_alert = stack.enter_context(patch.object(dm, "post_alert"))
            stack.enter_context(patch("sys.argv", ["dead_man.py"]))
            rc = dm.main()
        assert rc == 0
        mock_alert.assert_called_once()

    def test_dry_run_never_calls_restart_agent(self):
        with ExitStack() as stack:
            _patch_checks(stack, agent_ok=False)
            mock_restart = stack.enter_context(patch.object(dm, "restart_agent"))
            stack.enter_context(patch("sys.argv", ["dead_man.py", "--dry-run"]))
            rc = dm.main()
        assert rc == 0
        mock_restart.assert_not_called()
