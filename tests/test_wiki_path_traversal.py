"""Path-traversal security for tools/wiki/_shared.py."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


@pytest.fixture()
def fake_vault(tmp_path: Path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    import tools.wiki._shared as ws
    monkeypatch.setattr(ws, "VAULT_ROOT", vault)
    return vault


def test_wiki_append_refuses_traversal(fake_vault, tmp_path):
    from tools.wiki._shared import _do_wiki_append

    result = asyncio.run(_do_wiki_append({"path": "../../../tmp/poc", "content": "x"}))
    assert result.startswith("wiki: refused"), f"expected refusal, got: {result!r}"
    assert not (tmp_path / "poc.md").exists()
    assert not Path("/tmp/poc.md").exists()


def test_wiki_append_refuses_absolute_escape(fake_vault):
    from tools.wiki._shared import _do_wiki_append

    result = asyncio.run(_do_wiki_append({"path": "/etc/passwd", "content": "x"}))
    assert result.startswith("wiki: refused"), f"expected refusal, got: {result!r}"


def test_wiki_append_writes_legitimate_path(fake_vault):
    from tools.wiki._shared import _do_wiki_append

    (fake_vault / "notes").mkdir()
    asyncio.run(_do_wiki_append({"path": "notes/today", "content": "hello"}))
    target = fake_vault / "notes" / "today.md"
    assert target.exists(), f"file should have been created: {target}"
    assert "hello" in target.read_text()


def test_resolve_note_symlink_escape_skipped(fake_vault, tmp_path):
    from tools.wiki._shared import _resolve_note

    outside = tmp_path / "outside.md"
    outside.write_text("evil content")
    link = fake_vault / "escaped.md"
    link.symlink_to(outside)

    result = _resolve_note("escaped")
    assert result is None, (
        f"_resolve_note should return None for a symlink pointing outside the vault, "
        f"got: {result}"
    )
