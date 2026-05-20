"""Stream B regression: read_attachment must be hard-scoped to
data/user_photos/ and data/user_documents/. Anything outside those
roots must be refused — including path-traversal attempts.
"""
from __future__ import annotations

import pytest

from tools.attachments import REPO_ROOT, read_attachment


@pytest.mark.asyncio
async def test_absolute_outside_root_refused():
    """/etc/passwd is outside both allowed roots."""
    out = await read_attachment.handler({"path": "/etc/passwd"})
    text = out["content"][0]["text"]
    assert "refused" in text.lower()
    assert "data/user_photos/" in text or "data/user_documents/" in text


@pytest.mark.asyncio
async def test_path_traversal_refused():
    """../../etc/passwd resolves outside the allowed roots."""
    out = await read_attachment.handler({"path": "../../etc/passwd"})
    text = out["content"][0]["text"]
    assert "refused" in text.lower()


@pytest.mark.asyncio
async def test_nonexistent_file_refused():
    """A well-scoped path that does not exist returns 'not found'."""
    out = await read_attachment.handler(
        {"path": "data/user_photos/nonexistent_file_xyz.jpg"}
    )
    text = out["content"][0]["text"]
    assert "refused" in text.lower() or "not found" in text.lower()


@pytest.mark.asyncio
async def test_file_in_allowed_root_succeeds(tmp_path):
    """A file inside data/user_photos/ can be read."""
    allowed_dir = REPO_ROOT / "data" / "user_photos"
    allowed_dir.mkdir(parents=True, exist_ok=True)
    test_file = allowed_dir / "test_attachment_stream_f.txt"
    test_file.write_text("hello from the test", encoding="utf-8")
    try:
        out = await read_attachment.handler(
            {"path": str(test_file)}
        )
        text = out["content"][0]["text"]
        assert "hello from the test" in text
    finally:
        test_file.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_empty_path_refused():
    """Empty path string is refused immediately."""
    out = await read_attachment.handler({"path": ""})
    text = out["content"][0]["text"]
    assert "refused" in text.lower()
