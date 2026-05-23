"""Lazy MCP warm pool with per-server idle TTL.

Bucket-1 in-process MCP servers (hikari_*) are always attached.
Bucket-3 external MCPs (google_workspace, notion, github, playwright, etc.)
spawn ON FIRST acquire and shut down after idle TTL.

The pool is process-local (one per Hikari runtime). Subprocess lifecycle
matches the SDK's MCP child management — we do not directly spawn; we
only track WHICH MCPs should be attached to the next SDK options build.
"""
from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

_DEFAULT_TTL_S = 60


class McpManager:
    def __init__(self) -> None:
        # server_name -> last_acquired_epoch
        self._last_acquired: dict[str, float] = {}
        self._lock = asyncio.Lock()
        # per-server TTL config (seconds)
        self._ttl: dict[str, int] = {}

    def configure_ttls(self, ttls: dict[str, int]) -> None:
        self._ttl = dict(ttls)

    def _ttl_for(self, server_name: str) -> int:
        return int(self._ttl.get(server_name, _DEFAULT_TTL_S))

    async def acquire(self, server_name: str) -> None:
        """Mark a server as actively in use. Idempotent. Returns immediately."""
        async with self._lock:
            self._last_acquired[server_name] = time.time()
            logger.debug("mcp_manager: acquired %s", server_name)

    def is_warm(self, server_name: str) -> bool:
        last = self._last_acquired.get(server_name)
        if last is None:
            return False
        return (time.time() - last) < self._ttl_for(server_name)

    def warm_servers(self) -> set[str]:
        """Return the set of currently-warm server names."""
        now = time.time()
        return {
            name for name, last in self._last_acquired.items()
            if (now - last) < self._ttl_for(name)
        }

    async def evict_stale(self) -> list[str]:
        """Remove stale entries. Returns names of evicted servers."""
        now = time.time()
        evicted = []
        async with self._lock:
            for name in list(self._last_acquired.keys()):
                last = self._last_acquired[name]
                if (now - last) >= self._ttl_for(name):
                    del self._last_acquired[name]
                    evicted.append(name)
        if evicted:
            logger.info("mcp_manager: evicted stale %s", evicted)
        return evicted


MANAGER: McpManager = McpManager()


def configure_from_registry() -> None:
    """Load per-server warm_pool_ttl_sec from config/tools.yaml at boot."""
    try:
        from tools._tools_yaml import load_registry
        reg = load_registry()
        mcp_servers = reg.mcp_servers()
        ttls: dict[str, int] = {}
        for name, spec in mcp_servers.items():
            ttls[str(name)] = int(spec.warm_pool_ttl_sec)
        MANAGER.configure_ttls(ttls)
        logger.info("mcp_manager: configured TTLs for %d servers", len(ttls))
    except Exception:
        logger.exception("mcp_manager: failed to configure TTLs from registry")
