"""Provider registry and scope config loader.

Public API:
  load_scope_config() -> ScopeConfig   (module-level cache)
  reload_scope_config()                (for tests)
  get_provider(name) -> Provider
"""
from __future__ import annotations

import importlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Abstract Provider
# ---------------------------------------------------------------------------


class Provider(ABC):
    """Abstract OAuth / PAT provider."""

    name: str

    @abstractmethod
    async def current_scopes(self) -> set[str]:
        """Return the set of currently-granted scopes."""

    @abstractmethod
    async def refresh(self) -> str:
        """Refresh tokens; return new access token."""

    @abstractmethod
    def revoke(self) -> bool:
        """Revoke stored tokens. Returns True only if the underlying store operation succeeded."""


# ---------------------------------------------------------------------------
# Scope config dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ToolSpec:
    provider: str
    required_scopes: list[str]
    action: str


@dataclass
class ScopeConfig:
    tool_specs: dict[str, ToolSpec] = field(default_factory=dict)
    provider_templates: dict[str, str] = field(default_factory=dict)
    # raw provider config (provider_class, etc.)
    provider_config: dict[str, dict[str, Any]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Scope config loading
# ---------------------------------------------------------------------------

_scope_config: ScopeConfig | None = None


def load_scope_config() -> ScopeConfig:
    """Return the cached ScopeConfig; parse from tools.yaml registry on first call."""
    global _scope_config
    if _scope_config is not None:
        return _scope_config
    _scope_config = _parse_scope_config()
    return _scope_config


def reload_scope_config() -> ScopeConfig:
    """Force a reload (for tests / config changes)."""
    global _scope_config
    _scope_config = None
    return load_scope_config()


def _parse_scope_config() -> ScopeConfig:
    from tools._tools_yaml import load_registry
    registry = load_registry()

    cfg = ScopeConfig()

    # Provider config from auth_providers block in tools.yaml
    for prov_name, prov_data in registry.auth_providers().items():
        cfg.provider_config[prov_name] = prov_data
        cfg.provider_templates[prov_name] = prov_data.get("voice_template", "")

    # Tool scope specs from per-tool scopes blocks
    for spec in registry.specs():
        if spec.scopes_provider:
            cfg.tool_specs[spec.id] = ToolSpec(
                provider=spec.scopes_provider,
                required_scopes=list(spec.scopes_required),
                action=spec.scopes_action or "do that",
            )

    return cfg


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

_provider_instances: dict[str, Provider] = {}


def get_provider(name: str) -> Provider:
    """Return a cached Provider instance for the given provider name."""
    if name in _provider_instances:
        return _provider_instances[name]

    cfg = load_scope_config()
    prov_cfg = cfg.provider_config.get(name)
    if not prov_cfg:
        raise KeyError(f"auth: no provider config for '{name}'")

    class_path: str = prov_cfg["provider_class"]
    module_path, class_name = class_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)

    # Providers that need a store get one; PAT providers may not.
    try:
        from auth.store import default_store
        instance = cls(default_store())
    except TypeError:
        instance = cls()

    _provider_instances[name] = instance
    return instance


def _reset_providers() -> None:
    """For tests — clear cached provider instances."""
    global _provider_instances
    _provider_instances = {}


# ---------------------------------------------------------------------------
# PAT-based providers (Notion, GitHub)
# ---------------------------------------------------------------------------


class NotionProvider(Provider):
    """Notion integration token (PAT-style).

    Returns ``{"_present"}`` if NOTION_TOKEN env var is set; else empty set.
    """

    name = "notion"

    def __init__(self, _store=None) -> None:
        import os
        self._token = os.environ.get("NOTION_TOKEN") or ""

    async def current_scopes(self) -> set[str]:
        import os
        tok = os.environ.get("NOTION_TOKEN") or self._token
        return {"_present"} if tok else set()

    async def refresh(self) -> str:
        return self._token

    def revoke(self) -> bool:
        self._token = ""
        return True


class GitHubProvider(Provider):
    """GitHub personal access token.

    Returns ``{"_present"}`` if GITHUB_PERSONAL_ACCESS_TOKEN env var is set.
    """

    name = "github"

    def __init__(self, _store=None) -> None:
        import os
        self._token = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN") or ""

    async def current_scopes(self) -> set[str]:
        import os
        tok = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN") or self._token
        return {"_present"} if tok else set()

    async def refresh(self) -> str:
        return self._token

    def revoke(self) -> bool:
        self._token = ""
        return True
