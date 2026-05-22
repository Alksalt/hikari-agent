"""Tests for tools/wiki/list.py — wiki_tree (recursive, depth-limited)."""
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


@pytest.mark.asyncio
async def test_wiki_tree_recursive_walk(fake_vault):
    from tools.wiki.list import wiki_tree

    (fake_vault / "a").mkdir()
    (fake_vault / "a" / "b").mkdir()
    (fake_vault / "a" / "b" / "deep.md").write_text("# deep")
    (fake_vault / "a" / "top.md").write_text("# top")
    (fake_vault / "root.md").write_text("# root")

    result = await wiki_tree.handler({"path": "", "max_depth": 4})
    text = result["content"][0]["text"]

    assert "root.md" in text
    assert "top.md" in text
    assert "deep.md" in text
    assert "a/" in text or "a" in text


@pytest.mark.asyncio
async def test_wiki_tree_max_depth_respected(fake_vault):
    from tools.wiki.list import wiki_tree

    level1 = fake_vault / "l1"
    level1.mkdir()
    level2 = level1 / "l2"
    level2.mkdir()
    level3 = level2 / "l3"
    level3.mkdir()
    (level3 / "deep.md").write_text("# too deep")
    (level2 / "mid.md").write_text("# mid")

    result = await wiki_tree.handler({"path": "", "max_depth": 2})
    text = result["content"][0]["text"]

    assert "mid.md" in text
    assert "deep.md" not in text


@pytest.mark.asyncio
async def test_wiki_tree_truncates_past_200_entries(fake_vault):
    from tools.wiki.list import wiki_tree

    batch = fake_vault / "batch"
    batch.mkdir()
    for i in range(250):
        (batch / f"note_{i:04d}.md").write_text(f"# {i}")

    result = await wiki_tree.handler({"path": "", "max_depth": 4})
    text = result["content"][0]["text"]
    data = result["data"]

    assert data["truncated"] > 0
    assert "truncated" in text


@pytest.mark.asyncio
async def test_wiki_tree_skips_dotfiles(fake_vault):
    from tools.wiki.list import wiki_tree

    (fake_vault / ".hidden.md").write_text("# secret")
    pycache = fake_vault / "__pycache__"
    pycache.mkdir()
    (pycache / "something.md").write_text("# cache")
    (fake_vault / "visible.md").write_text("# visible")

    result = await wiki_tree.handler({"path": "", "max_depth": 4})
    text = result["content"][0]["text"]

    assert ".hidden.md" not in text
    assert "__pycache__" not in text
    assert "visible.md" in text


@pytest.mark.asyncio
async def test_wiki_tree_refuses_traversal(fake_vault):
    from tools.wiki.list import wiki_tree

    result = await wiki_tree.handler({"path": "../etc", "max_depth": 3})
    text = result["content"][0]["text"]
    assert "refused" in text
