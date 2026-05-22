"""Tests for _build_ingest_block — plain text / csv / md branch."""
from __future__ import annotations

import pytest

from agents.telegram_bridge import _build_ingest_block


def test_md_inlined(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text("# heading\n\nsome content here", encoding="utf-8")
    block, kind_note = _build_ingest_block(path, "text/plain", "notes.md")

    assert block is not None
    assert block["type"] == "text"
    assert "### inlined text — notes.md" in block["text"]
    assert "some content here" in block["text"]
    assert "text file inlined" in kind_note


def test_csv_inlined(tmp_path):
    path = tmp_path / "data.csv"
    path.write_text("col1,col2\n1,2\n3,4", encoding="utf-8")
    block, kind_note = _build_ingest_block(path, "text/csv", "data.csv")

    assert block is not None
    assert block["type"] == "text"
    assert "### inlined text — data.csv" in block["text"]
    assert "col1,col2" in block["text"]


def test_text_truncated_at_64k(tmp_path):
    path = tmp_path / "big.txt"
    content = "x" * 70_000
    path.write_text(content, encoding="utf-8")
    block, kind_note = _build_ingest_block(path, "text/plain", "big.txt")

    assert block is not None
    assert "truncated" in block["text"]
    # The actual text content should be at most 64000 chars + marker overhead
    assert len(block["text"]) < 70_000 + 200
