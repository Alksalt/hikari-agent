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
