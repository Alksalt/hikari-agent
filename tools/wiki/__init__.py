"""Obsidian wiki feature — manifest.

DEDICATED MCP SERVER. ``agents/runtime.py`` does
``from tools import wiki as wiki_tools`` and registers
``wiki_tools.PUBLIC_TOOLS`` against an in-process ``hikari_wiki`` server
(see ``agents/runtime.py:_wiki_server``). The shared registry skips
``wiki`` on purpose (see ``tools/_registry.py:_DEDICATED_SERVER_MODULES``)
so this package is NOT auto-discovered into the utility server.

The vault lives in iCloud Drive, so reads materialize files via
``brctl download`` before touching them. Writes use python-frontmatter +
ruamel.yaml to preserve key order and avoid churn on every save. Graph
queries (backlinks, wikilink resolution) go through ``obsidiantools``.

Re-exports:
- ``VAULT_ROOT`` — referenced by ``agents/reflection.py`` (morning dispatch)
  and patched in ``tests/test_morning_dispatch.py`` via ``wiki_mod.VAULT_ROOT``.
- ``wiki_search`` — ``mcp_external/server.py`` imports it directly to
  proxy the local handler over the external MCP surface.
- Internal helpers (``_icloud_materialize``, ``_vault``, ``_resolve_note``,
  ``_do_wiki_append``) re-exported for symmetry with the prior flat module.
"""
from __future__ import annotations

from tools.wiki._shared import (  # noqa: F401 — back-compat re-exports
    VAULT_ROOT,
    _do_wiki_append,
    _icloud_materialize,
    _resolve_note,
    _vault,
)
from tools.wiki.append import wiki_append
from tools.wiki.backlinks import wiki_backlinks
from tools.wiki.list import wiki_list, wiki_tree
from tools.wiki.morning_brief import morning_brief_tool
from tools.wiki.read import wiki_read
from tools.wiki.search import wiki_search

# Public tools — registered on the always-on ``hikari_wiki`` MCP server.
# These are the tools Sonnet can see on every turn (subject to allowlist).
# wiki_append no longer requires approval (iCloud history is the safety net).
PUBLIC_TOOLS = [wiki_search, wiki_read, wiki_append, wiki_backlinks, wiki_list, wiki_tree, morning_brief_tool]

# Phase 8: no privileged wiki tools. ``CONFIRMED_TOOLS`` retained for
# back-compat (empty list) so any importer using the symbol doesn't break.
CONFIRMED_TOOLS: list = []

# Backwards-compat alias — some imports may reference the flat list.
ALL_TOOLS = PUBLIC_TOOLS
