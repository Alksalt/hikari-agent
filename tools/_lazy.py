"""Lazy tool builder.

Why this exists: most tool handlers pull in heavy deps (httpx, arxiv,
pandas, etc.). With ``@tool`` declared at module-import time, those deps
load at boot whether the tool is ever called or not. Multiply by dozens
of features and we pay the import cost up front for everything.

``lazy_tool(...)`` builds a thin ``@tool`` stub whose body only imports
the real handler module on first invocation. The stub itself is cheap —
no heavy imports — so a feature folder's ``__init__.py`` can declare all
its tools without pulling anything but stdlib.

Usage::

    from tools._lazy import lazy_tool

    link_save = lazy_tool(
        name="link_save",
        description="Save a URL to the shelf...",
        schema={"url": str, "kind": str, "tags": list, "note": str},
        impl="tools.link_shelf.handlers:save",
    )

The ``impl`` field is ``module_path:function_name``. The handler must be
an ``async def fn(args: dict) -> dict`` returning the standard
``tools._response.ok(...)`` envelope.
"""
from __future__ import annotations

import importlib
from typing import Any

from claude_agent_sdk import tool


def lazy_tool(
    *,
    name: str,
    description: str,
    schema: dict[str, Any],
    impl: str,
):
    """Build a ``@tool``-decorated stub that defers handler import.

    Returns the decorated callable, ready to be added to a feature's
    ``ALL_TOOLS`` list.
    """
    if ":" not in impl:
        raise ValueError(
            f"lazy_tool impl must be 'module.path:function_name', got {impl!r}"
        )
    module_path, func_name = impl.split(":", 1)

    @tool(name, description, schema)
    async def _stub(args: dict[str, Any]) -> dict[str, Any]:
        mod = importlib.import_module(module_path)
        fn = getattr(mod, func_name)
        return await fn(args)

    _stub.__name__ = f"lazy_{name}"
    _stub.__doc__ = description
    return _stub
