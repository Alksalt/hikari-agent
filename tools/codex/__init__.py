"""Codex reports feature — manifest.

DEDICATED MCP SERVER. ``agents/runtime.py`` does
``from tools import codex as codex_tools`` and registers
``codex_tools.ALL_TOOLS`` against an in-process ``hikari_codex`` server.
The shared registry skips ``codex`` on purpose (see
``tools/_registry.py:_DEDICATED_SERVER_MODULES``) so this package is
NOT auto-discovered into the utility server. Keep ``ALL_TOOLS``
accessible at ``tools.codex.ALL_TOOLS``.

Re-exports: the module-level constants and private path helpers from
``_shared.py`` so tests that reach into the package namespace
(e.g. ``codex_tools._MAX_READ_BYTES``) keep working.
"""
from __future__ import annotations

from tools.codex._shared import (  # noqa: F401 — back-compat re-exports
    _MAX_LIMIT,
    _MAX_READ_BYTES,
    _reports_dir,
    _safe_name,
)
from tools.codex.list_reports import list_codex_reports
from tools.codex.read_report import read_codex_report

ALL_TOOLS = [list_codex_reports, read_codex_report]
