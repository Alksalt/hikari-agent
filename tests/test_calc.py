"""Phase 10: calc + python_run sandbox."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from agents import config
from storage import db


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    config.reload()
    yield

@pytest.mark.asyncio
async def test_calc_basic_arithmetic():
    from tools import calc
    out = await calc.calc.handler({"expr": "17.5 * 2400 / 100"})
    assert out["data"]["result"] == 420.0

@pytest.mark.asyncio
async def test_calc_handles_division_by_zero():
    from tools import calc
    out = await calc.calc.handler({"expr": "1/0"})
    assert "err" in out["content"][0]["text"].lower() or "zero" in out["content"][0]["text"].lower()

@pytest.mark.asyncio
async def test_calc_blocks_file_access():
    from tools import calc
    out = await calc.calc.handler({"expr": "open('/etc/passwd').read()"})
    # asteval should refuse open() — error or None result
    assert out["data"].get("result") is None or "err" in out["content"][0]["text"].lower()

@pytest.mark.asyncio
async def test_calc_blocks_import():
    """R1 finding: asteval's Interpreter(minimal=False) exposed __import__.
    The strip-list must include it so __import__('os').system(...) fails."""
    from tools import calc
    out = await calc.calc.handler(
        {"expr": "__import__('os').system('echo escaped > /tmp/_calc_pwn')"}
    )
    assert out["data"].get("result") is None or "err" in out["content"][0]["text"].lower()
    import os.path
    assert not os.path.exists("/tmp/_calc_pwn"), "calc sandbox escape: __import__ executed"

@pytest.mark.asyncio
async def test_calc_blocks_getattr_introspection():
    """getattr can be chained to reach __import__ via type lookups."""
    from tools import calc
    out = await calc.calc.handler({"expr": "getattr(object, '__class__')"})
    assert out["data"].get("result") is None or "err" in out["content"][0]["text"].lower()

@pytest.mark.asyncio
async def test_calc_date_arithmetic():
    from tools import calc
    out = await calc.calc.handler({
        "expr": "(date(2026, 5, 19) - date(2026, 1, 1)).days"
    })
    assert out["data"]["result"] == 138

@pytest.mark.asyncio
async def test_python_run_returns_stdout():
    from tools import calc
    out = await calc.python_run.handler({"code": "print(2 + 2)"})
    assert "4" in out["data"]["stdout"]

@pytest.mark.asyncio
async def test_python_run_blocks_network(monkeypatch):
    from tools import calc
    out = await calc.python_run.handler({
        "code": "import urllib.request; urllib.request.urlopen('http://example.com')"
    })
    assert out["data"]["returncode"] != 0 or "example.com" not in out["data"].get("stdout", "")


# ---- Phase 11 P0 regressions ----

@pytest.mark.asyncio
async def test_calc_works_from_thread_pool():
    """Fix 1: previous SIGALRM-based timeout crashed when called off the main
    thread with `ValueError: signal only works in main thread`. Calling calc
    from a worker thread must now succeed."""
    import asyncio

    from tools import calc

    # asyncio.to_thread runs the body on a default-pool worker thread.
    out = await asyncio.to_thread(
        asyncio.run, calc.calc.handler({"expr": "2 + 2"})
    )
    assert out["data"]["result"] == 4


@pytest.mark.asyncio
async def test_python_run_blocks_subprocess_exec():
    """Fix 2: the sandbox profile must stop LLM code from spawning a child
    process via subprocess. Without this, the LLM could shell out to a binary
    like /usr/bin/curl that opens its own network socket from outside the
    sandboxed Python process."""
    from tools import calc
    snippet = (
        "import subprocess\n"
        "r = subprocess.run(['/bin/echo', 'hi'], capture_output=True)\n"
        "print('rc=', r.returncode)\n"
        "print('out=', r.stdout)\n"
    )
    out = await calc.python_run.handler({"code": snippet})
    text_out = out["data"].get("stdout", "") + out["data"].get("stderr", "")
    assert out["data"]["returncode"] != 0, (
        f"expected non-zero exit from blocked subprocess, got: {text_out!r}"
    )
    assert "Operation not permitted" in text_out or "PermissionError" in text_out, (
        f"expected PermissionError from blocked fork, got: {text_out!r}"
    )


@pytest.mark.asyncio
async def test_python_run_blocks_curl_exfil():
    """Fix 2: the worst-case exfil path — spawning /usr/bin/curl which would
    open its own socket bypassing the network deny on the Python process.
    Must fail at fork time."""
    from tools import calc
    snippet = (
        "import subprocess\n"
        "r = subprocess.run(['/usr/bin/curl', '-s', 'http://example.com'], "
        "capture_output=True, timeout=2)\n"
        "print('rc=', r.returncode)\n"
        "print(r.stdout[:50])\n"
    )
    out = await calc.python_run.handler({"code": snippet})
    text_out = out["data"].get("stdout", "") + out["data"].get("stderr", "")
    assert "Example Domain" not in out["data"].get("stdout", ""), (
        f"curl exfil succeeded — sandbox escape: {text_out!r}"
    )
    assert out["data"]["returncode"] != 0, (
        f"expected non-zero exit from blocked curl, got: {text_out!r}"
    )


@pytest.mark.asyncio
async def test_calc_blocks_dunder_class_chain():
    """Fix 3: dunder attribute chain must be rejected at AST-parse time so the
    classic (1).__class__.__mro__[-1].__subclasses__() escape can't be walked
    even if a future asteval version loosens its own attribute filter."""
    from tools import calc
    out = await calc.calc.handler({"expr": "(1).__class__"})
    assert out["data"].get("result") is None
    text = out["content"][0]["text"].lower()
    assert "attribute chain rejected" in text or "err" in text, (
        f"expected dunder rejection, got: {out['content'][0]['text']!r}"
    )


@pytest.mark.asyncio
async def test_calc_allows_legit_attribute_access():
    """Fix 3 regression guard: legitimate non-dunder attribute access
    (.days on a timedelta, .year on a date) must still work after the dunder
    gate is added."""
    from tools import calc
    out = await calc.calc.handler({
        "expr": "(date(2026, 5, 19) - date(2026, 1, 1)).days"
    })
    assert out["data"]["result"] == 138, (
        f"legit attribute access broken: {out['content'][0]['text']!r}"
    )
    # Also confirm .year still works.
    out2 = await calc.calc.handler({"expr": "date(2026, 5, 19).year"})
    assert out2["data"]["result"] == 2026
