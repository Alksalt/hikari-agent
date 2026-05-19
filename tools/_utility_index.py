"""Phase 10: aggregates all utility tools into a single ALL_TOOLS list for the
``hikari_utility`` MCP server. Each feature appends its own tools here during
Phase 1 parallel work; conflicts during merge are trivial concats.

Order doesn't matter — the MCP server doesn't care."""

from __future__ import annotations

# Imports are appended by each Phase 1 worktree. Keep them sorted for
# merge-friendliness.
from tools.arxiv_search import ALL_TOOLS as _ARXIV
from tools.calc import ALL_TOOLS as _CALC
from tools.currency import ALL_TOOLS as _CURRENCY
from tools.places import ALL_TOOLS as _PLACES
from tools.reminders import ALL_TOOLS as _REMINDERS
from tools.translate import ALL_TOOLS as _TRANSLATE
from tools.weather import ALL_TOOLS as _WEATHER

from tools.ytmusic import ALL_TOOLS as _YTMUSIC

ALL_TOOLS: list = []
ALL_TOOLS.extend(_ARXIV)
ALL_TOOLS.extend(_CALC)
ALL_TOOLS.extend(_CURRENCY)
ALL_TOOLS.extend(_PLACES)
ALL_TOOLS.extend(_REMINDERS)
ALL_TOOLS.extend(_TRANSLATE)
ALL_TOOLS.extend(_WEATHER)
ALL_TOOLS.extend(_YTMUSIC)
