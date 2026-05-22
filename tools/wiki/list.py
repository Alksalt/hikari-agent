"""wiki_list (one level) + wiki_tree (recursive, depth-limited).

Both bypass the obsidiantools Vault graph cache (_vault) — straight
filesystem walks via pathlib so newly-synced iCloud notes appear
immediately without restart-on-change."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from claude_agent_sdk import tool

from tools._response import ok as _ok
from tools.wiki._shared import VAULT_ROOT

_MAX_ENTRIES = 200
_MAX_DEPTH = 4


def _entries_at(path: Path) -> tuple[list[dict], list[dict]]:
    """Return (folders, files) for one directory level. .md only for files;
    folders include subitem counts so the model knows where to drill in."""
    folders, files = [], []
    if not path.exists() or not path.is_dir():
        return folders, files
    for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if child.name.startswith(".") or child.name == "__pycache__":
            continue
        if child.is_dir():
            try:
                md_count = sum(1 for _ in child.rglob("*.md"))
            except OSError:
                md_count = 0
            folders.append({"name": child.name, "md_count": md_count})
        elif child.suffix.lower() == ".md":
            try:
                mtime = datetime.fromtimestamp(
                    child.stat().st_mtime, tz=UTC
                ).isoformat()
            except OSError:
                mtime = None
            files.append({"name": child.name, "mtime": mtime})
    return folders, files


@tool(
    "wiki_list",
    "List the immediate contents of one folder in the user's Obsidian wiki "
    "by relative path (or empty string for the vault root). Returns folders "
    "(with md counts) and .md files (with mtimes). Always fresh — does NOT "
    "use the vault graph cache. Use this when the user asks 'what's in "
    "<folder>' or 'is there anything new in X'. For a multi-level view use "
    "wiki_tree.",
    {"path": str},
)
async def wiki_list(args: dict[str, Any]) -> dict[str, Any]:
    rel = (args.get("path") or "").strip().strip("/")
    target = VAULT_ROOT if not rel else VAULT_ROOT / rel
    try:
        target = target.resolve()
        target.relative_to(VAULT_ROOT.resolve())
    except (ValueError, OSError):
        return _ok(f"wiki_list: refused — path outside vault: {rel!r}")
    folders, files = _entries_at(target)
    label = rel or "<vault root>"
    lines = [f"contents of {label}:"]
    if folders:
        lines.append("folders:")
        for f in folders:
            lines.append(f"  {f['name']}/  ({f['md_count']} md)")
    if files:
        lines.append("files:")
        for f in files:
            lines.append(f"  {f['name']}  (mtime {f['mtime']})")
    if not folders and not files:
        lines.append("  (empty)")
    return _ok(
        "\n".join(lines),
        data={"path": rel, "folders": folders, "files": files},
        presentation_hint="wiki_tree",
        sources=[{
            "name": "wiki:fs",
            "url": str(target),
            "fetched_at": datetime.now(UTC).isoformat(),
            "confidence": 1.0,
        }],
    )


@tool(
    "wiki_tree",
    "Recursively list .md files under a folder up to max_depth (default 4). "
    "Always fresh — straight filesystem walk, no vault graph. Emits a "
    "tree-shaped summary. If the total entry count exceeds 200, the result "
    "is truncated and a note records how many were dropped.",
    {"path": str, "max_depth": int},
)
async def wiki_tree(args: dict[str, Any]) -> dict[str, Any]:
    rel = (args.get("path") or "").strip().strip("/")
    max_depth = max(1, min(8, int(args.get("max_depth") or _MAX_DEPTH)))
    try:
        root = (VAULT_ROOT if not rel else VAULT_ROOT / rel).resolve()
        root.relative_to(VAULT_ROOT.resolve())
    except (ValueError, OSError):
        return _ok(f"wiki_tree: refused — path outside vault: {rel!r}")
    entries: list[tuple[int, str, bool]] = []
    truncated = 0

    def _walk(p: Path, depth: int) -> None:
        nonlocal truncated
        if depth > max_depth:
            return
        try:
            children = sorted(p.iterdir(), key=lambda q: (not q.is_dir(), q.name.lower()))
        except OSError:
            return
        for c in children:
            if c.name.startswith(".") or c.name == "__pycache__":
                continue
            if c.is_file() and c.suffix.lower() != ".md":
                continue
            if len(entries) >= _MAX_ENTRIES:
                truncated += 1
                continue
            entries.append((depth, c.name, c.is_dir()))
            if c.is_dir():
                _walk(c, depth + 1)

    _walk(root, 0)
    lines = [f"tree under {rel or '<vault root>'} (depth ≤ {max_depth}):"]
    for depth, name, is_dir in entries:
        prefix = "  " * depth + ("\U0001f4c1 " if is_dir else "")
        lines.append(f"{prefix}{name}{'/' if is_dir else ''}")
    notes = []
    if truncated:
        notes.append(f"truncated — {truncated} entries dropped (cap {_MAX_ENTRIES}); ask for a subtree")
    return _ok(
        "\n".join(lines),
        data={"path": rel, "max_depth": max_depth,
              "entries": entries, "truncated": truncated},
        presentation_hint="wiki_tree",
        notes=notes or None,
        sources=[{
            "name": "wiki:fs",
            "url": str(root),
            "fetched_at": datetime.now(UTC).isoformat(),
            "confidence": 1.0,
        }],
    )


ALL_TOOLS = [wiki_list, wiki_tree]
