"""Project-level test fixtures."""
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _block_graphiti(request):
    """Prevent any test from accidentally hitting real Graphiti/Kuzu paths.

    Tests marked with ``pytest.mark.uses_real_graph`` opt out of this fixture
    and manage their own graph patches (e.g. test_graph_phase_c.py)."""
    if request.node.get_closest_marker("uses_real_graph"):
        yield
        return
    with (
        patch(
            "storage.graph.get_graph",
            new=AsyncMock(side_effect=RuntimeError("graph blocked in tests")),
        ),
        patch("storage.graph.add_episode_safe", new=AsyncMock(return_value=False)),
        patch("storage.graph.schedule_episode", new=lambda *a, **kw: False),
    ):
        yield


@pytest.fixture(autouse=True)
def _block_live_mcp_calls(request):
    """Prevent any test from accidentally spawning a real MCP subprocess.

    Added after an incident where unmocked ``collect_sections()`` tests made a
    REAL Gmail query and appended a REAL line to the owner's outreach repo's
    handoff file (live google_workspace credentials were reachable on the dev
    machine). Tests that patch a module-level ``MANAGER`` binding (the
    established pattern, e.g. test_typed_gmail_adapter.py) are unaffected —
    this only blocks calls that reach the real ``McpManager.call``.

    Tests marked with ``pytest.mark.uses_real_mcp`` opt out of this fixture
    and manage their own MCP access."""
    if request.node.get_closest_marker("uses_real_mcp"):
        yield
        return
    with patch(
        "agents.mcp_manager.McpManager.call",
        new=AsyncMock(side_effect=RuntimeError("live MCP call blocked in tests")),
    ):
        yield
