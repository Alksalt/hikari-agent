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


def test_resolve_note_bare_stem_prefers_vault_root(fake_vault):
    """Bare stem 'log' must resolve to VAULT_ROOT/log.md even when a nested
    VAULT_ROOT/projects/x/log.md also exists (regression: rglob ordering was
    non-deterministic and could return the nested file first)."""
    from tools.wiki._shared import _resolve_note

    # Create both files so rglob would have an ambiguous set to return from.
    root_log = fake_vault / "log.md"
    root_log.write_text("root log")
    nested_dir = fake_vault / "projects" / "x"
    nested_dir.mkdir(parents=True)
    nested_log = nested_dir / "log.md"
    nested_log.write_text("nested log")

    result = _resolve_note("log")
    assert result == root_log.resolve(), (
        f"expected VAULT_ROOT/log.md ({root_log.resolve()}), got {result}"
    )


def test_resolve_note_explicit_subpath_hits_nested(fake_vault):
    """'projects/x/log' must resolve to the nested file even when VAULT_ROOT/log.md
    also exists — an explicit subdir path is honoured over the root file."""
    from tools.wiki._shared import _resolve_note

    root_log = fake_vault / "log.md"
    root_log.write_text("root log")
    nested_dir = fake_vault / "projects" / "x"
    nested_dir.mkdir(parents=True)
    nested_log = nested_dir / "log.md"
    nested_log.write_text("nested log")

    result = _resolve_note("projects/x/log")
    assert result == nested_log.resolve(), (
        f"expected nested log ({nested_log.resolve()}), got {result}"
    )
