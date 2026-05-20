"""Scoped attachment reader — only reads from data/user_photos/ and
data/user_documents/. Replaces unscoped Read/Glob/Grep for the
photo/voice/document handlers."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from claude_agent_sdk import tool

from tools._response import ok as _ok

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
ALLOWED_ROOTS = (
    REPO_ROOT / "data" / "user_photos",
    REPO_ROOT / "data" / "user_documents",
)

_MAX_BYTES = 8 * 1024 * 1024  # 8 MiB cap


@tool(
    "read_attachment",
    (
        "Read an inbound user attachment (photo or document) by relative or "
        "absolute path. Hard-scoped: paths MUST resolve under "
        "data/user_photos/ or data/user_documents/. Returns the file "
        "content; image files come back as base64 with a content-type tag, "
        "text files come back as utf-8. Anything outside the allowed roots "
        "is refused. This is the ONLY way to read user-supplied files; "
        "general filesystem Read/Glob/Grep are not available."
    ),
    {"path": str},
)
async def read_attachment(args: dict[str, Any]) -> dict[str, Any]:
    raw = (args.get("path") or "").strip()
    if not raw:
        return _ok("refused: empty path")
    try:
        candidate = (REPO_ROOT / raw).resolve() if not os.path.isabs(raw) else Path(raw).resolve()
    except (OSError, ValueError) as e:
        return _ok(f"refused: path resolve failed ({e})")
    # Hard containment check — every allowed root must be a parent.
    for root in ALLOWED_ROOTS:
        try:
            candidate.relative_to(root.resolve())
            break
        except ValueError:
            continue
    else:
        logger.warning("read_attachment: refused path outside allowed roots: %s", candidate)
        return _ok("refused: path must be under data/user_photos/ or data/user_documents/")
    if not candidate.exists():
        return _ok(f"refused: {candidate.name} not found")
    if not candidate.is_file():
        return _ok("refused: not a regular file")
    try:
        size = candidate.stat().st_size
    except OSError as e:
        return _ok(f"refused: stat failed ({e})")
    if size > _MAX_BYTES:
        return _ok(f"refused: file too large ({size} bytes; cap {_MAX_BYTES})")
    suffix = candidate.suffix.lower()
    try:
        if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}:
            import base64
            raw_bytes = candidate.read_bytes()
            b64 = base64.b64encode(raw_bytes).decode("ascii")
            return _ok(
                f"[image/{suffix.lstrip('.')}; base64; {len(raw_bytes)} bytes]\n{b64}",
                data={"size": len(raw_bytes), "kind": "image", "suffix": suffix},
            )
        text = candidate.read_text(encoding="utf-8", errors="replace")
        return _ok(text, data={"size": size, "kind": "text", "suffix": suffix})
    except OSError as e:
        logger.exception("read_attachment: read failed for %s", candidate)
        return _ok(f"refused: read failed ({e})")


ALL_TOOLS = [read_attachment]
