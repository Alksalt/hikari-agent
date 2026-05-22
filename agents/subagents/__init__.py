"""Specialist subagents Hikari delegates to via the `Agent` tool.

Phase A (step 7): subagent definitions are now generated from
``config/tools.yaml`` via ``tools._tools_yaml.load_registry().subagents()``.
The ``recall`` and ``code_dispatch`` subagents have been removed — their
functionality is served by direct tool calls (mcp__hikari_memory__recall,
mcp__hikari_dispatch__dispatch_claude_session).

``ALL_AGENTS`` is kept as a property-backed dict for backwards-compat with
callers that do ``from agents.subagents import ALL_AGENTS``. It delegates to
the registry on every access so that test monkeypatching of the registry path
still works.
"""

from __future__ import annotations

from claude_agent_sdk import AgentDefinition


def _get_all_agents() -> dict[str, AgentDefinition]:
    from tools._tools_yaml import load_registry
    return load_registry().subagents()


class _AllAgentsProxy(dict):
    """Lazy-loading dict that delegates to the registry on first access.

    Acts as a regular dict once populated. Tests that monkeypatch the registry
    path will see the updated values because each test import cycle re-calls
    _get_all_agents().
    """

    def __init__(self):
        super().__init__()
        self._loaded = False

    def _ensure_loaded(self):
        if not self._loaded:
            self.update(_get_all_agents())
            self._loaded = True

    def __getitem__(self, key):
        self._ensure_loaded()
        return super().__getitem__(key)

    def __iter__(self):
        self._ensure_loaded()
        return super().__iter__()

    def __len__(self):
        self._ensure_loaded()
        return super().__len__()

    def __contains__(self, key):
        self._ensure_loaded()
        return super().__contains__(key)

    def items(self):
        self._ensure_loaded()
        return super().items()

    def keys(self):
        self._ensure_loaded()
        return super().keys()

    def values(self):
        self._ensure_loaded()
        return super().values()

    def get(self, key, default=None):
        self._ensure_loaded()
        return super().get(key, default)


ALL_AGENTS: dict[str, AgentDefinition] = _AllAgentsProxy()

__all__ = ["ALL_AGENTS"]
