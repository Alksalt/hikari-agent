"""Verify that blocking subprocess/eval calls don't stall the event loop.

A concurrent asyncio.sleep(0.05) must complete within ~200ms while a
slow calc evaluation is running. If calc blocks the event loop the
sleep would be delayed far beyond its scheduled time.
"""
from __future__ import annotations

import asyncio
import importlib
import sys
import time
from pathlib import Path

import pytest

from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield


@pytest.mark.asyncio
async def test_calc_does_not_block_event_loop(monkeypatch):
    """A slow asteval expression must not block asyncio.sleep running concurrently.

    The @tool decorator replaces the calc.py module-level name with an SdkMcpTool;
    the underlying async function is accessed via .handler. _run_asteval is imported
    into the calc module's closure and runs inside asyncio.to_thread — patching the
    _shared module's attribute ensures the thread-worker sees the mock.
    """
    import tools.calc._shared as _shared_mod

    def slow_eval(expr, timeout):
        time.sleep(0.3)
        return (42, None)

    monkeypatch.setattr(_shared_mod, "_run_asteval", slow_eval)

    # Force the calc module's local reference to update by reloading the module
    # after patching _shared. This is necessary because `from X import fn` creates
    # a local binding that doesn't track subsequent patches of X.fn.
    calc_py_module = sys.modules.get("tools.calc.calc")
    if calc_py_module is not None and hasattr(calc_py_module, "_run_asteval"):
        monkeypatch.setattr(calc_py_module, "_run_asteval", slow_eval)

    from tools.calc.calc import calc as calc_tool

    sleep_start = asyncio.get_event_loop().time()
    sleep_done_at: list[float] = []

    async def track_sleep():
        await asyncio.sleep(0.05)
        sleep_done_at.append(asyncio.get_event_loop().time())

    # calc_tool is an SdkMcpTool; call its handler directly.
    await asyncio.gather(
        calc_tool.handler({"expr": "1+1"}),
        track_sleep(),
    )

    sleep_elapsed = sleep_done_at[0] - sleep_start
    # The sleep must complete well within the 300ms blocking window.
    # Give 200ms headroom for CI jitter.
    assert sleep_elapsed < 0.20, (
        f"sleep took {sleep_elapsed:.3f}s — event loop was likely blocked"
    )


@pytest.mark.asyncio
async def test_calc_result_still_returned():
    """After off-loading to a thread, calc returns the correct result."""
    from tools.calc.calc import calc as calc_tool

    result = await calc_tool.handler({"expr": "2 + 2"})
    content = result.get("content", [])
    texts = [blk.get("text", "") for blk in content if blk.get("type") == "text"]
    assert any("4" in t for t in texts), f"unexpected result: {result}"
