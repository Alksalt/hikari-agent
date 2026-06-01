"""9B: /status OAuth truthfulness — probe_google_token is called, TTL cached."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_cache():
    """Clear the probe cache between tests."""
    import agents.cockpit as cockpit_mod
    cockpit_mod._OAUTH_PROBE_CACHE.clear()
    yield
    cockpit_mod._OAUTH_PROBE_CACHE.clear()


@pytest.mark.asyncio
async def test_oauth_states_calls_probe():
    """_oauth_states() must call probe_google_token and return its result."""
    probe = AsyncMock(return_value=(True, ""))
    with patch("agents.google_health.probe_google_token", probe):
        from agents.cockpit import _oauth_states
        result = await _oauth_states()

    probe.assert_awaited_once()
    assert result["google"] == "ok"


@pytest.mark.asyncio
async def test_oauth_cache_ttl_avoids_second_probe():
    """Three consecutive calls must only invoke probe once (within TTL)."""
    probe = AsyncMock(return_value=(True, ""))
    with patch("agents.google_health.probe_google_token", probe):
        from agents.cockpit import _oauth_states
        await _oauth_states()
        await _oauth_states()
        await _oauth_states()

    assert probe.await_count == 1


@pytest.mark.asyncio
async def test_oauth_cache_refresh_after_ttl():
    """After TTL expires, the probe is called again."""
    import agents.cockpit as cockpit_mod

    probe = AsyncMock(return_value=(True, ""))
    with patch("agents.google_health.probe_google_token", probe):
        from agents.cockpit import _oauth_states
        await _oauth_states()
        # Force expiry by back-dating the cache entry.
        cockpit_mod._OAUTH_PROBE_CACHE["google"] = ("ok", time.time() - 999)
        await _oauth_states()

    assert probe.await_count == 2
