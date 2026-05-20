# tools/

Each tool gets its own `.py` file. Multi-tool features (and most
single-tool features for uniformity) live in folders auto-discovered by
`tools/_registry.py`. Dropping a folder is enough — no edits to the
runtime allowlist or utility index.

## Canonical layout (one tool per file)

Reference: `tools/apple_notes/` and `tools/link_shelf/`.

```
tools/foo/
├── __init__.py     # manifest — re-exports + ALL_TOOLS
├── _shared.py      # helpers + constants shared across tools (optional)
├── do_a.py         # one @tool per file (action verb in filename, not prefixed tool name)
├── do_b.py
├── _db.py          # self-contained DB schema + CRUD (optional)
└── README.md       # what this feature does (optional)
```

```python
# tools/foo/__init__.py
"""Foo feature — manifest.

Re-exports tool callables for the registry + any module attributes the
tests monkey-patch through this package's namespace.
"""
from __future__ import annotations

# Re-export stdlib modules ONLY when tests patch via this package
# (e.g. ``foo.asyncio.create_subprocess_exec``). Otherwise skip.
# import asyncio  # noqa: F401 — test patch target

from tools.foo._shared import _some_helper  # noqa: F401 — re-export if a test imports it directly
from tools.foo.do_a import foo_a
from tools.foo.do_b import foo_b

ALL_TOOLS = [foo_a, foo_b]
```

```python
# tools/foo/do_a.py
from claude_agent_sdk import tool
from tools._response import ok as _ok
from tools.foo._shared import _some_helper

@tool("foo_a", "Do A. ...", {"x": str})
async def foo_a(args):
    # heavy imports (httpx, pandas, sdk clients) go HERE — not module top
    import httpx  # noqa: PLC0415
    ...
    return _ok("done", data={...})
```

## Single-tool features

A folder with one tool file is still a folder — keeps the repo shape
uniform. The action-verb filename rule still applies (`tools/foo/do.py`
defines `@tool("foo_do", ...)`).

## Conventions

- The manifest (top-level `__init__.py` or flat module) must expose a
  `ALL_TOOLS: list` containing the `@tool`-decorated callables.
- Names starting with `_` are skipped by discovery (`_response`,
  `_lazy`, `_registry`, `_utility_index`).
- The following packages are wired to dedicated MCP servers, not the
  utility server, and the registry skips them on purpose: `memory`,
  `photos`, `wiki`, `dispatch`, `codex`. If you're adding a tool that
  needs its own server, talk to me first.
- Tool names are flat (no module prefix). The MCP server adds the
  `mcp__hikari_utility__` prefix at registration. Don't collide.
- Heavy deps (httpx, pandas, numpy, network SDK clients) belong inside
  handler functions, not at module top. With `lazy_tool`, the manifest
  doesn't even import the handlers module until first call.

## Self-contained DB schema

If your feature needs tables, follow the `tools/link_shelf/db.py`
pattern:
- declare `_SCHEMA` as a string of `CREATE TABLE IF NOT EXISTS`
  statements;
- gate it behind a process-level `_SCHEMA_INITIALIZED` sentinel +
  lock so it only runs once per process;
- open connections via `storage.db._conn` (the shared SQLite
  contextmanager handles WAL, sqlite_vec, etc.).

That way features stay decoupled from the central `storage/db.py`
schema and you can iterate on your feature without touching shared
files.

## Allowlist

You do not need to add your tool to `agents/runtime.py`. The
`_base_allowed_tools()` function derives the utility allowlist from
the registry automatically.

## Tests

Add tests under `tests/test_<feature>.py`. Use the
`HIKARI_DB_PATH` + `db._reset_schema_sentinel()` fixture pattern from
`tests/test_link_shelf.py` to get an isolated SQLite per test.
