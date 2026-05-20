"""calc feature — manifest.

Two tools share this package because they cover the same job (run a
small piece of math/python the user just asked about) at two different
risk levels: ``calc`` for safe in-process expressions, ``python_run``
for snippets that need a real interpreter under macOS sandbox-exec.
Shared defensive constants live in ``_shared.py``.
"""
from __future__ import annotations

from tools.calc._shared import _run_asteval  # noqa: F401 — internal helper, re-exported for direct test access
from tools.calc.calc import calc
from tools.calc.python_run import python_run

ALL_TOOLS = [calc, python_run]
