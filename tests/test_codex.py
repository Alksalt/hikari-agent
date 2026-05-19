"""Phase 8 — tools/codex.py MCP tools (list + read).

Covers:
  - list returns mtime-ordered .md files
  - read returns wrapped untrusted content
  - missing file → clear error message
  - directory traversal blocked (.. attempts stay inside the configured dir)
  - oversized files → size-cap message instead of read
  - .md extension auto-appended when omitted
  - empty / missing directory → graceful empty response
"""

from __future__ import annotations

import importlib
import time
from pathlib import Path

import pytest

from agents import config
from storage import db
from tools import codex as codex_tools


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    config.reload()
    yield


def _setup_reports_dir(tmp_path: Path, monkeypatch) -> Path:
    """Create a tmp reports dir and point config at it via env override."""
    reports = tmp_path / "codex"
    reports.mkdir()
    cfg_text = (
        "codex:\n"
        f"  reports_dir: {reports}\n"
        "prompt_injection:\n"
        "  enabled: true\n"
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()
    return reports


@pytest.mark.asyncio
async def test_list_returns_mtime_ordered(tmp_path, monkeypatch):
    reports = _setup_reports_dir(tmp_path, monkeypatch)
    # Create three files with distinct mtimes.
    (reports / "alpha.md").write_text("alpha body")
    time.sleep(0.02)
    (reports / "beta.md").write_text("beta body")
    time.sleep(0.02)
    (reports / "gamma.md").write_text("gamma body")

    result = await codex_tools.list_codex_reports.handler({"limit": 10})
    body = result["content"][0]["text"]
    # Newest first.
    assert body.index("gamma.md") < body.index("beta.md") < body.index("alpha.md")
    assert result["data"]["reports"][0]["name"] == "gamma.md"


@pytest.mark.asyncio
async def test_list_respects_limit(tmp_path, monkeypatch):
    reports = _setup_reports_dir(tmp_path, monkeypatch)
    for i in range(5):
        (reports / f"r{i}.md").write_text(f"body {i}")
        time.sleep(0.01)

    result = await codex_tools.list_codex_reports.handler({"limit": 2})
    assert len(result["data"]["reports"]) == 2


@pytest.mark.asyncio
async def test_list_empty_dir(tmp_path, monkeypatch):
    _setup_reports_dir(tmp_path, monkeypatch)
    result = await codex_tools.list_codex_reports.handler({"limit": 5})
    assert "no .md reports found" in result["content"][0]["text"]
    assert result["data"]["reports"] == []


@pytest.mark.asyncio
async def test_list_missing_dir(tmp_path, monkeypatch):
    """Pointing at a nonexistent dir returns a clear message, not a crash."""
    missing = tmp_path / "does_not_exist"
    cfg_text = f"codex:\n  reports_dir: {missing}\n"
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()

    result = await codex_tools.list_codex_reports.handler({"limit": 5})
    assert "does not exist" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_read_returns_wrapped_content(tmp_path, monkeypatch):
    reports = _setup_reports_dir(tmp_path, monkeypatch)
    body = "# review\n\nthis is the codex finding"
    (reports / "alpha.md").write_text(body)

    result = await codex_tools.read_codex_report.handler({"name": "alpha.md"})
    text = result["content"][0]["text"]
    assert "<<<HIKARI_UNTRUSTED_BEGIN>>>" in text
    assert "<<<HIKARI_UNTRUSTED_END>>>" in text
    assert "this is the codex finding" in text
    assert result["data"]["untrusted"] is True


@pytest.mark.asyncio
async def test_read_appends_md_extension(tmp_path, monkeypatch):
    reports = _setup_reports_dir(tmp_path, monkeypatch)
    (reports / "alpha.md").write_text("content")
    result = await codex_tools.read_codex_report.handler({"name": "alpha"})
    assert "<<<HIKARI_UNTRUSTED_BEGIN>>>" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_read_missing_file(tmp_path, monkeypatch):
    _setup_reports_dir(tmp_path, monkeypatch)
    result = await codex_tools.read_codex_report.handler({"name": "nope.md"})
    assert "not found" in result["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_read_requires_name(tmp_path, monkeypatch):
    _setup_reports_dir(tmp_path, monkeypatch)
    result = await codex_tools.read_codex_report.handler({"name": ""})
    assert "required" in result["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_read_blocks_directory_traversal(tmp_path, monkeypatch):
    """``../`` components in name must not escape the reports dir."""
    reports = _setup_reports_dir(tmp_path, monkeypatch)
    # File outside the reports dir.
    (tmp_path / "secret.md").write_text("not for codex")
    (reports / "ok.md").write_text("inside")

    # The safe-name strip + relative_to check should kill the traversal.
    result = await codex_tools.read_codex_report.handler(
        {"name": "../secret.md"},
    )
    # Reads the *baseified* path under the reports dir — which doesn't exist.
    body = result["content"][0]["text"]
    assert "not found" in body.lower() or "outside" in body.lower()
    assert "not for codex" not in body


@pytest.mark.asyncio
async def test_read_size_cap(tmp_path, monkeypatch):
    reports = _setup_reports_dir(tmp_path, monkeypatch)
    big = "x" * (codex_tools._MAX_READ_BYTES + 100)
    (reports / "big.md").write_text(big)

    result = await codex_tools.read_codex_report.handler({"name": "big.md"})
    body = result["content"][0]["text"]
    assert "bytes (max" in body or "too large" in body.lower() or "max" in body


@pytest.mark.asyncio
async def test_list_ignores_non_md_files(tmp_path, monkeypatch):
    reports = _setup_reports_dir(tmp_path, monkeypatch)
    (reports / "report.md").write_text("md")
    (reports / "notes.txt").write_text("txt")
    (reports / "data.json").write_text("{}")

    result = await codex_tools.list_codex_reports.handler({"limit": 10})
    names = [r["name"] for r in result["data"]["reports"]]
    assert names == ["report.md"]
