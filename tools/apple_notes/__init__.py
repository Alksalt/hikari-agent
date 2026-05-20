"""Apple Notes feature — manifest.

One file per tool (``create.py`` / ``search.py`` / ``read.py``), shared
osascript + escaping helpers in ``_shared.py``.

Re-exports: ``_as_quoted`` (test dependency — verifies AppleScript
escaping in isolation), and the stdlib ``asyncio`` + ``sys`` modules
that ``tests/test_apple_notes.py`` monkey-patches via
``apple_notes.asyncio`` and ``apple_notes.sys``. The patches target the
live ``asyncio.create_subprocess_exec`` and ``sys.platform`` so the
test mock is observed by ``_run_osascript`` in ``_shared.py``.
"""
from __future__ import annotations

import asyncio  # noqa: F401 — re-exported for test patching
import sys  # noqa: F401 — re-exported for test patching

from tools.apple_notes._shared import _as_quoted  # noqa: F401 — test dependency
from tools.apple_notes.create import note_create
from tools.apple_notes.read import note_read
from tools.apple_notes.search import note_search

ALL_TOOLS = [note_create, note_search, note_read]
