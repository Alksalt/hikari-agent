"""Router feature — manifest.

DEDICATED MCP SERVER. ``agents/runtime.py`` registers
``router_tools.ALL_TOOLS`` against an in-process ``hikari_router`` server.
The shared registry skips ``router`` (it is listed in
``tools._registry._DEDICATED_SERVER_MODULES``) so this package is NOT
auto-discovered into the utility server.

Boot: call ``rebuild_index()`` once so the BM25 corpus is ready before
the first user turn.
"""
from __future__ import annotations

from tools.router.tool_search import rebuild_index, tool_search

ALL_TOOLS = [tool_search]

__all__ = ["ALL_TOOLS", "rebuild_index", "tool_search"]
