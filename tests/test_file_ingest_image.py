"""Tests for _build_ingest_block — image branch (JPEG + HEIC fallback)."""
from __future__ import annotations

import base64

from agents.telegram_bridge import _build_ingest_block

# Minimal valid JPEG: SOI + EOI markers
_MINIMAL_JPEG = bytes([0xFF, 0xD8, 0xFF, 0xE0, 0xFF, 0xD9])


def test_jpeg_inline_block(tmp_path):
    path = tmp_path / "photo.jpg"
    path.write_bytes(_MINIMAL_JPEG)
    block, kind_note = _build_ingest_block(path, "image/jpeg", "photo.jpg")

    assert block is not None
    assert block["type"] == "image"
    src = block["source"]
    assert src["type"] == "base64"
    assert src["media_type"] == "image/jpeg"
    decoded = base64.b64decode(src["data"])
    assert decoded == _MINIMAL_JPEG
    assert "image" in kind_note.lower()


def test_heic_falls_back_when_no_pil_heif(tmp_path):
    """HEIC conversion requires pillow-heif. Without it Image.open on raw HEIC
    bytes fails and the helper falls back to (None, '...read_attachment...')."""
    path = tmp_path / "photo.heic"
    # Write some plausible HEIC magic bytes (ftypheic box header)
    path.write_bytes(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 24)

    try:
        import pillow_heif  # noqa: F401
        # pillow-heif is installed — the conversion might actually succeed.
        # In that case the block will not be None; skip this specific assertion.
        block, kind_note = _build_ingest_block(path, "image/heic", "photo.heic")
        if block is not None:
            # Conversion succeeded — verify it produced an image/png block
            assert block["type"] == "image"
            assert block["source"]["media_type"] == "image/png"
        else:
            assert "read_attachment" in kind_note
    except ImportError:
        # No pillow-heif → Image.open will fail → must fall back
        block, kind_note = _build_ingest_block(path, "image/heic", "photo.heic")
        assert block is None
        assert "read_attachment" in kind_note
