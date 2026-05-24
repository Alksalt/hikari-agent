"""``list_codex_reports`` — directory listing of saved Codex audits.

Returns ``.md`` files under the configured ``codex.reports_dir``
newest-first with sizes. This is a static directory listing, not a
live review run.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from claude_agent_sdk import tool

from tools._annotations import annotations_for
from tools._response import ok as _ok
from tools.codex._shared import _MAX_LIMIT, _reports_dir

logger = logging.getLogger(__name__)


@tool(
    "list_codex_reports",
    "List the Codex review report files under this repo's codex/ dir (newest first, "
    "with sizes). Codex reports are static markdown audits already generated and "
    "saved to disk — this is a directory listing, not a live review. "
    "e.g. user asks 'what did codex flag' or 'are there any review reports' → "
    "list first, then `read_codex_report` for the chosen filename. "
    "Don't use this to run a new review or to read arbitrary files (use `Read`).",
    {"limit": int},
    annotations=annotations_for("list_codex_reports"),
)
async def list_codex_reports(args: dict[str, Any]) -> dict[str, Any]:
    limit = max(1, min(_MAX_LIMIT, int(args.get("limit") or 10)))
    base = _reports_dir()
    if not base.exists() or not base.is_dir():
        return _ok(
            f"codex: reports dir {base} does not exist.",
            data={"reports": []},
        )

    candidates: list[tuple[float, Path]] = []
    for p in base.glob("*.md"):
        if not p.is_file():
            continue
        try:
            candidates.append((p.stat().st_mtime, p))
        except OSError:
            continue
    candidates.sort(key=lambda kv: -kv[0])
    chosen = candidates[:limit]

    if not chosen:
        return _ok(
            "codex: no .md reports found.",
            data={"reports": []},
        )

    lines = [f"{len(chosen)} codex report(s) (newest first):"]
    payload: list[dict[str, Any]] = []
    for mtime, path in chosen:
        try:
            size = path.stat().st_size
        except OSError:
            size = -1
        rel = path.name
        lines.append(f"  - {rel} ({size} bytes)")
        payload.append({"name": rel, "size": size, "mtime": mtime})

    return _ok("\n".join(lines), data={"reports": payload})
