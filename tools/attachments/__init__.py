"""Attachments feature — manifest.

Single-tool feature: ``read_attachment`` is the only way for handlers to
read user-supplied files. Hard-scoped to ``data/user_photos/`` and
``data/user_documents/``.

Re-exports: ``REPO_ROOT`` and ``ALLOWED_ROOTS`` (test dependency —
``tests/test_read_attachment_path_validation.py`` imports ``REPO_ROOT``
to write a fixture file inside an allowed root).
"""
from __future__ import annotations

from tools.attachments.read import (  # noqa: F401 — re-exports for tests/registry
    ALLOWED_ROOTS,
    REPO_ROOT,
    read_attachment,
)

ALL_TOOLS = [read_attachment]
