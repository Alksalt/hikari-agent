"""Aggregator for the ``hikari_utility`` MCP server.

Auto-discovers every feature under ``tools/`` (flat module ``tools/<name>.py``
or package ``tools/<name>/__init__.py``) that exposes ``ALL_TOOLS``. Drop
a new folder, restart — no edits here.

See ``tools/_registry.py`` for the discovery rules and ``tools/README.md``
for the feature-folder convention.
"""

from __future__ import annotations

from tools._registry import discover_utility_tools


def _all_tools() -> list:
    return discover_utility_tools()


# Property-like access via module attribute so callers can keep writing
# ``from tools import _utility_index; _utility_index.ALL_TOOLS``. The
# discovery is cached, so this stays cheap on repeated access.
def __getattr__(name: str):
    if name == "ALL_TOOLS":
        return _all_tools()
    raise AttributeError(f"module 'tools._utility_index' has no attribute {name!r}")
