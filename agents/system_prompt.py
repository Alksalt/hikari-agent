"""Secure materialization of Hikari's Claude system prompt.

The Claude Agent SDK maps a string ``system_prompt`` to a literal CLI argument.
Hikari's prompt contains a per-install injection canary, so the live path uses
the SDK's file form instead and keeps only a non-secret path in process argv.
"""

from __future__ import annotations

import atexit
import os
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SYSTEM_PROMPT_DIR = REPO_ROOT / "data" / "runtime"


def _cleanup_prompt_file(path: Path) -> None:
    """Best-effort removal of one process-private prompt file at clean exit."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def materialize_system_prompt(
    prompt: str,
    *,
    directory: Path | None = None,
) -> dict[str, str]:
    """Write *prompt* to an immutable process-private file for the Claude CLI."""
    parent = directory or SYSTEM_PROMPT_DIR
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parent.is_symlink():
        raise RuntimeError(f"system prompt directory must not be a symlink: {parent}")
    parent.chmod(0o700)

    target = parent / f"system_prompt.{os.getpid()}.{uuid.uuid4().hex}.md"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    fd = os.open(target, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(prompt)
            handle.flush()
            os.fsync(handle.fileno())
        target.chmod(0o600)
    except Exception:
        target.unlink(missing_ok=True)
        raise

    atexit.register(_cleanup_prompt_file, target)
    return {"type": "file", "path": str(target)}
