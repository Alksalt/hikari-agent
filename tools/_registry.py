"""Auto-discovery for utility-tool features.

A "feature" is either:
  - a flat module ``tools/<name>.py`` that exposes ``ALL_TOOLS``, or
  - a package ``tools/<name>/__init__.py`` that exposes ``ALL_TOOLS``.

Adding a new feature is supposed to be "drop a folder, restart" — no
edits to ``_utility_index.py`` or ``agents/runtime.py``. This registry
walks ``tools/`` once per process and collects everything that looks
like a tool manifest.

Skipped by convention:
  - any name starting with ``_`` (private helpers like ``_response``,
    ``_lazy``, ``_registry``, ``_utility_index``)
  - ``__pycache__`` and similar
  - features whose imports fail are logged and skipped (a broken
    feature should never take down the whole agent)

The registry is cached. Tests that need to re-scan after dropping a new
feature in a temp dir can call ``clear_cache()``.
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from functools import cache
from typing import Any

logger = logging.getLogger(__name__)

# Modules under ``tools/`` that exist for infrastructure reasons and don't
# expose any user-facing tools. Listed explicitly so a typo doesn't get a
# free pass. Anything else without ``ALL_TOOLS`` is logged at debug level
# and skipped.
_INFRASTRUCTURE_MODULES: set[str] = {
    "_response",
    "_lazy",
    "_registry",
    "_utility_index",
}

# Subpackages that are NOT utility-server features and must not be merged
# into ``hikari_utility``. They live in ``tools/`` because they're
# conceptually tool code, but they're wired to dedicated MCP servers by
# ``agents/runtime.py`` (memory, photos, wiki, dispatch, codex).
_DEDICATED_SERVER_MODULES: set[str] = {
    "memory",
    "photos",
    "wiki",
    "dispatch",
    "codex",
}


def _is_skipped(name: str) -> bool:
    if name.startswith("_"):
        return True
    if name in _DEDICATED_SERVER_MODULES:
        return True
    return False


@cache
def _discover_utility_tools_cached() -> tuple:
    """Internal cache layer. Returns an immutable tuple so the public
    ``discover_utility_tools`` can hand callers a fresh list each call
    without risking cache poisoning if someone mutates the result.
    """
    import tools  # local import — avoid circulars at module load

    collected: list = []
    for modinfo in pkgutil.iter_modules(tools.__path__):
        name = modinfo.name
        if _is_skipped(name):
            continue
        full_name = f"tools.{name}"
        try:
            module = importlib.import_module(full_name)
        except Exception:  # noqa: BLE001 — never break the whole server
            logger.exception("tool feature %s failed to import; skipping", full_name)
            continue
        feature_tools = getattr(module, "ALL_TOOLS", None)
        if feature_tools is None:
            logger.debug("tool module %s has no ALL_TOOLS; skipping", full_name)
            continue
        if not isinstance(feature_tools, list):
            logger.warning(
                "tool module %s ALL_TOOLS is %s, expected list; skipping",
                full_name, type(feature_tools).__name__,
            )
            continue
        collected.extend(feature_tools)
    return tuple(collected)


def discover_utility_tools() -> list:
    """Walk ``tools/`` and return the merged ALL_TOOLS for hikari_utility.

    Cached at module level. Failures inside a single feature are
    logged and skipped — they don't propagate. Each call returns a
    fresh list copy; callers can mutate freely.
    """
    return list(_discover_utility_tools_cached())


@cache
def _discover_utility_tool_names_cached() -> tuple:
    """Internal cache layer mirroring ``_discover_utility_tools_cached``."""
    names: list[str] = []
    for t in discover_utility_tools():
        tool_name = _extract_tool_name(t)
        if tool_name is None:
            continue
        names.append(f"mcp__hikari_utility__{tool_name}")
    return tuple(names)


def discover_utility_tool_names() -> list[str]:
    """Return the fully-qualified MCP names for every discovered tool.

    Format: ``mcp__hikari_utility__<tool_name>`` — matches the allowlist
    entries in ``agents/runtime.py``. Used to auto-derive that allowlist
    from the registry instead of hand-maintaining it. Each call returns
    a fresh list copy.
    """
    return list(_discover_utility_tool_names_cached())


def _extract_tool_name(t: Any) -> str | None:
    """Pull the registered MCP tool name out of an SDK ``@tool`` callable.

    The SDK attaches the schema to the wrapped function; the exact
    attribute name varies across SDK versions, so we look in a few
    plausible places before giving up. We never raise — a tool we can't
    introspect just doesn't get auto-allowlisted.
    """
    for attr in ("name", "_tool_name", "tool_name"):
        val = getattr(t, attr, None)
        if isinstance(val, str) and val:
            return val
    schema = getattr(t, "schema", None) or getattr(t, "_schema", None)
    if isinstance(schema, dict):
        val = schema.get("name")
        if isinstance(val, str) and val:
            return val
    return None


def clear_cache() -> None:
    """Test helper — clears the discovery cache so a fresh import sweep
    runs on next access. Real callers should never need this."""
    _discover_utility_tools_cached.cache_clear()
    _discover_utility_tool_names_cached.cache_clear()
