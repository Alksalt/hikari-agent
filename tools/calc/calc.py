"""``calc`` — in-process asteval expression evaluator.

Fast, no subprocess. Math, list comprehensions, datetime arithmetic.
For pandas-style work or anything requiring imports, use ``python_run``.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from claude_agent_sdk import tool

from agents import config as cfg
from tools._annotations import annotations_for
from tools._response import ok as _ok
from tools.calc._shared import _run_asteval

logger = logging.getLogger(__name__)


@tool(
    "calc",
    "Evaluate a Python expression (math, list comp, date arithmetic). "
    "Safe: no imports, no file/network access, no statements. Returns the value. "
    "Examples: '17.5 * 2400 / 100', '(date(2026,5,19) - date(2026,1,1)).days', "
    "'sum(range(100))'.",
    {"expr": str},
    annotations=annotations_for("calc"),
)
async def calc(args: dict[str, Any]) -> dict[str, Any]:
    expr = (args.get("expr") or "").strip()
    if not expr:
        return _ok("refused: empty expression")
    timeout = float(cfg.get("calc.timeout_sec", 5))
    # _run_asteval blocks inside a ThreadPoolExecutor; run it off the event
    # loop so concurrent asyncio tasks aren't stalled during evaluation.
    result, err = await asyncio.to_thread(_run_asteval, expr, timeout)
    if err:
        return _ok(f"err: {err}", data={"result": None, "error": err})
    return _ok(f"{result!r}", data={"result": result})
