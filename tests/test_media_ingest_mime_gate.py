"""MIME magic-byte allowlist in _build_ingest_block.

Validates that _check_magic_bytes rejects files whose raw bytes don't match
their declared MIME type, and accepts files that do match.
"""
from __future__ import annotations

import pytest


def _check(raw: bytes, mime: str) -> bool:
    from agents.telegram_bridge import _check_magic_bytes
    return _check_magic_bytes(raw, mime)


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def test_pdf_valid_header():
    assert _check(b"%PDF-1.4\nrest of file", "application/pdf") is True


def test_pdf_mz_exe_rejected():
    """PE executable disguised as PDF must be rejected."""
    assert _check(b"MZ" + b"\x00" * 100, "application/pdf") is False


def test_pdf_wrong_magic():
    assert _check(b"\x89PNG\r\n\x1a\n", "application/pdf") is False


def test_pdf_empty_rejected():
    assert _check(b"", "application/pdf") is False


# ---------------------------------------------------------------------------
# JPEG
# ---------------------------------------------------------------------------

def test_jpeg_valid():
    assert _check(b"\xff\xd8\xff\xe0" + b"\x00" * 10, "image/jpeg") is True


def test_jpeg_wrong_magic():
    assert _check(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10, "image/jpeg") is False


# ---------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------

def test_png_valid():
    assert _check(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10, "image/png") is True


def test_png_wrong_magic():
    assert _check(b"GIF89a" + b"\x00" * 10, "image/png") is False


# ---------------------------------------------------------------------------
# GIF
# ---------------------------------------------------------------------------

def test_gif87a_valid():
    assert _check(b"GIF87a" + b"\x00" * 10, "image/gif") is True


def test_gif89a_valid():
    assert _check(b"GIF89a" + b"\x00" * 10, "image/gif") is True


def test_gif_wrong_magic():
    assert _check(b"\xff\xd8\xff" + b"\x00" * 10, "image/gif") is False


# ---------------------------------------------------------------------------
# WebP
# ---------------------------------------------------------------------------

def test_webp_valid():
    raw = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 20
    assert _check(raw, "image/webp") is True


def test_webp_not_riff():
    raw = b"JFIF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 20
    assert _check(raw, "image/webp") is False


def test_webp_riff_but_not_webp():
    raw = b"RIFF" + b"\x00\x00\x00\x00" + b"AVI " + b"\x00" * 20
    assert _check(raw, "image/webp") is False


def test_webp_too_short():
    raw = b"RIFF" + b"\x00\x00\x00"  # only 7 bytes — can't check offset 8
    assert _check(raw, "image/webp") is False


# ---------------------------------------------------------------------------
# Text (not magic-byte checked — always passes)
# ---------------------------------------------------------------------------

def test_text_plain_always_passes():
    assert _check(b"hello world", "text/plain") is True


def test_application_json_always_passes():
    assert _check(b"{}", "application/json") is True
