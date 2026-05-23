"""Memory feature — manifest.

DEDICATED MCP SERVER. ``agents/runtime.py`` does
``from tools import memory as memory_tools`` and registers
``memory_tools.ALL_TOOLS`` against an in-process ``hikari_memory`` server.
The shared registry skips ``memory`` on purpose (see
``tools/_registry.py:_DEDICATED_SERVER_MODULES``) so this package is
NOT auto-discovered into the utility server. Keep ``ALL_TOOLS``
accessible at ``tools.memory.ALL_TOOLS``.

Re-exports ``storage.retrieval`` at the package namespace because
``tests/test_engagement_memory.py`` does
``monkeypatch.setattr(mem_mod.retrieval, "retrieve", ...)`` and that
patch only lands if ``recall`` resolves ``retrieval`` through this
package's namespace.
"""
from __future__ import annotations

from storage import retrieval  # noqa: F401 — re-exported for test monkey-patching
from tools.memory.mark_fact_invalid import mark_fact_invalid
from tools.memory.recall import recall
from tools.memory.remember import remember
from tools.memory.session_search import session_search
from tools.memory.task_create import task_create
from tools.memory.task_update import task_update
from tools.memory.update_core_block import update_core_block

ALL_TOOLS = [recall, remember, mark_fact_invalid, update_core_block, task_create, task_update,
             session_search]
