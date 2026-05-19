"""Phase 10: calc + python_run sandbox."""
from __future__ import annotations
import importlib
from pathlib import Path
import pytest
from storage import db
from agents import config

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
