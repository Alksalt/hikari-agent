"""Tests for tools/wiki/list.py — wiki_list (one level) and path-traversal guard."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def fake_vault(tmp_path: Path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    import tools.wiki.list as wl
    monkeypatch.setattr(wl, "VAULT_ROOT", vault)
    return vault


# ---------------------------------------------------------------------------
# wiki_list
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wiki_list_returns_root_entries(fake_vault):
    from tools.wiki.list import wiki_list

    (fake_vault / "alpha").mkdir()
    (fake_vault / "alpha" / "note1.md").write_text("# A")
    (fake_vault / "alpha" / "note2.md").write_text("# B")
    (fake_vault / "beta").mkdir()
    (fake_vault / "beta" / "sub.md").write_text("# C")
    (fake_vault / "root.md").write_text("# R")
    (fake_vault / "second.md").write_text("# S")
    (fake_vault / "third.md").write_text("# T")

    result = await wiki_list.handler({"path": ""})
    text = result["content"][0]["text"]

    assert "alpha/" in text
    assert "beta/" in text
    assert "root.md" in text
    assert "second.md" in text
    assert "third.md" in text

    data = result["data"]
    folder_names = [f["name"] for f in data["folders"]]
    assert "alpha" in folder_names
    assert "beta" in folder_names

    alpha_entry = next(f for f in data["folders"] if f["name"] == "alpha")
    assert alpha_entry["md_count"] == 2

    beta_entry = next(f for f in data["folders"] if f["name"] == "beta")
    assert beta_entry["md_count"] == 1


@pytest.mark.asyncio
async def test_wiki_list_returns_subdir_entries(fake_vault):
    from tools.wiki.list import wiki_list

    subdir = fake_vault / "projects"
    subdir.mkdir()
    (subdir / "foo.md").write_text("# Foo")
    (subdir / "bar.md").write_text("# Bar")
    nested = subdir / "nested"
    nested.mkdir()

    result = await wiki_list.handler({"path": "projects"})
    data = result["data"]

    file_names = [f["name"] for f in data["files"]]
    assert "foo.md" in file_names
    assert "bar.md" in file_names

    folder_names = [f["name"] for f in data["folders"]]
    assert "nested" in folder_names


@pytest.mark.asyncio
async def test_wiki_list_refuses_traversal(fake_vault):
    from tools.wiki.list import wiki_list

    result = await wiki_list.handler({"path": "../etc"})
    text = result["content"][0]["text"]
    assert "refused" in text


@pytest.mark.asyncio
async def test_wiki_list_handles_empty_dir(fake_vault):
    from tools.wiki.list import wiki_list

    (fake_vault / "empty_dir").mkdir()
    result = await wiki_list.handler({"path": "empty_dir"})
    text = result["content"][0]["text"]
    assert "(empty)" in text
