"""Tests for _build_ingest_block — unsupported mime types."""
from __future__ import annotations

from agents.telegram_bridge import _build_ingest_block


def test_unsupported_mime_returns_none_block(tmp_path):
    path = tmp_path / "archive.zip"
    path.write_bytes(b"PK\x03\x04" + b"\x00" * 20)
    block, kind_note = _build_ingest_block(path, "application/octet-stream", "archive.zip")

    assert block is None
    assert "unsupported mime" in kind_note
    assert "application/octet-stream" in kind_note
