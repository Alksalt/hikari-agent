"""Native CLI tool deferral must be OFF unless explicitly re-enabled.

Deferred tool schemas caused the 2026-07-04 reminder_create({}) loop —
see plans/2026-07-04-sonnet5-adaptation.md Task 1 and
alt-wiki tech/claude-agent-sdk-patterns.md §8.
"""
from unittest.mock import patch

from agents import config as cfg
from agents.runtime import _build_options


def test_tool_search_disabled_by_default():
    opts = _build_options(resume=None)
    assert opts.env.get("ENABLE_TOOL_SEARCH") == "false"


def test_tool_search_can_be_reenabled_via_cfg():
    # agents.runtime.cfg IS this same agents.config module object, so
    # patching "agents.runtime.cfg.get" replaces get() on the one shared
    # module. Capture the original callable before patching — calling
    # cfg.get(...) from inside the fake would recurse into the mock.
    original_get = cfg.get

    def fake_get(key, default=None):
        if key == "runtime.tool_search_enabled":
            return True
        return original_get(key, default)

    with patch("agents.runtime.cfg.get", side_effect=fake_get):
        opts = _build_options(resume=None)
    assert "ENABLE_TOOL_SEARCH" not in opts.env
