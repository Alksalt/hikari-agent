"""Tests for _build_ingest_block — PDF branch."""
from __future__ import annotations

import base64

from agents.telegram_bridge import _build_ingest_block


def _fake_pdf(tmp_path) -> tuple:
    p = tmp_path / "test.pdf"
    p.write_bytes(b"%PDF-1.4\nfake pdf content\n%%EOF\n")
    return p, "application/pdf", "test.pdf"


def test_pdf_inline_block_shape(tmp_path):
    path, mime, fname = _fake_pdf(tmp_path)
    block, kind_note = _build_ingest_block(path, mime, fname)

    assert block is not None
    assert block["type"] == "document"
    src = block["source"]
    assert src["type"] == "base64"
    assert src["media_type"] == "application/pdf"
    # data must be valid base64 and round-trip back to original bytes
    decoded = base64.b64decode(src["data"])
    assert decoded == path.read_bytes()
    assert "pdf" in kind_note.lower()


def test_mz_guard_exists_in_source():
    """The MZ-prefix guard is a string in handle_document source — verify it's present."""
    import inspect

    from agents import telegram_bridge
    src = inspect.getsource(telegram_bridge.handle_document)
    assert "MZ" in src, "Magic-byte guard for MZ (PE executable) missing from handle_document"
