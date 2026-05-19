"""Phase 10: aggregates all utility tools into a single ALL_TOOLS list for the
``hikari_utility`` MCP server. Each feature appends its own tools here during
Phase 1 parallel work; conflicts during merge are trivial concats.

Order doesn't matter — the MCP server doesn't care."""

from __future__ import annotations

# Imports are appended by each Phase 1 worktree. Keep them sorted for
# merge-friendliness.

ALL_TOOLS: list = []

from tools.translate import ALL_TOOLS as _TRANSLATE
ALL_TOOLS.extend(_TRANSLATE)
