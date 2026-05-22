"""Shared helpers + constants for the Obsidian wiki tools.

The vault lives in iCloud Drive, so reads must materialize files via
``brctl download`` before touching them. Writes use python-frontmatter +
ruamel.yaml to preserve key order and avoid churn on every save.

Graph queries (backlinks, wikilink resolution) go through
``obsidiantools``' networkx graph, which is built once at module load and
cached via ``lru_cache``. Long-term we may rebuild on watchdog FS events;
for now, restart-on-change is acceptable.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import frontmatter
from obsidiantools.api import Vault
from ruamel.yaml import YAML

logger = logging.getLogger(__name__)


# ---- upstream bug workaround ------------------------------------------------
# obsidiantools' _get_md_front_matter_and_content (md_utils.py:248) assigns
# `file_string` INSIDE its try block, then references it from a bare `except:`
# clause. If `open()` itself fails (PermissionError, OSError) or `f.read()`
# raises (UnicodeDecodeError on non-UTF-8 files), `file_string` is never bound
# and the except branch raises UnboundLocalError instead of returning ({}, "").
# That error propagates out of Vault.gather() and crashes wiki_search before
# our own try/except can catch it. We replace the function with a safe version.
def _patch_obsidiantools_md_utils() -> None:
    def _safe_read(filepath, *, str_transform_func=None):
        try:
            with open(filepath, encoding="utf-8") as f:
                content = f.read()
        except UnicodeDecodeError:
            try:
                with open(filepath, encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except Exception:  # noqa: BLE001
                return ({}, "")
        except Exception:  # noqa: BLE001
            return ({}, "")
        if str_transform_func:
            try:
                content = str_transform_func(content)
            except Exception:  # noqa: BLE001
                pass
        try:
            return frontmatter.parse(content)
        except Exception:  # noqa: BLE001 — yaml errors etc.
            return ({}, content)

    # Patch BOTH known import sites: md_utils (defining module) AND api
    # (which does ``from .md_utils import _get_md_front_matter_and_content``
    # at module load, creating an independent name binding inside the api
    # namespace). The api binding is what Vault.gather() actually calls.
    for mod_path in ("obsidiantools.md_utils", "obsidiantools.api"):
        try:
            import importlib
            m = importlib.import_module(mod_path)
            if hasattr(m, "_get_md_front_matter_and_content"):
                m._get_md_front_matter_and_content = _safe_read
        except Exception:  # noqa: BLE001
            continue


_patch_obsidiantools_md_utils()

VAULT_ROOT = Path(
    os.environ.get("HIKARI_WIKI_VAULT")
    or Path.home() / "Library/Mobile Documents/iCloud~md~obsidian/Documents/alt-wiki"
).expanduser()

_yaml = YAML(typ="rt")
_yaml.preserve_quotes = True
_yaml.indent(mapping=2, sequence=4, offset=2)


def _brctl_download(path: Path, timeout: int) -> None:
    """Blocking brctl call. Meant to be run via asyncio.to_thread."""
    try:
        subprocess.run(
            ["brctl", "download", str(path)],
            check=False, timeout=timeout, capture_output=True,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("brctl download failed for %s: %s", path, e)


async def _icloud_materialize(path: Path, timeout: int = 30) -> None:
    """Force iCloud to download a placeholder file. No-op if already materialized.
    Runs brctl off the event loop via asyncio.to_thread."""
    if not path.exists() and not path.with_name(f".{path.name}.icloud").exists():
        return
    await asyncio.to_thread(_brctl_download, path, timeout)


_VAULT_CACHE: tuple[Any, float] | None = None
_VAULT_TTL_SEC: float = 300


def _vault() -> Vault:
    """Build (or return a cached) vault graph. Rebuilt after TTL expires."""
    global _VAULT_CACHE
    now = time.monotonic()
    if _VAULT_CACHE is not None and (now - _VAULT_CACHE[1]) < _VAULT_TTL_SEC:
        return _VAULT_CACHE[0]
    logger.info("connecting to obsidian vault at %s", VAULT_ROOT)
    v = Vault(VAULT_ROOT).connect().gather()
    logger.info("vault ready: %d notes, %d backlinks", len(v.md_file_index), sum(
        1 for _ in v.backlinks_index.values()
    ))
    _VAULT_CACHE = (v, now)
    return v


def invalidate_vault() -> None:
    """Drop the cached Vault so the next _vault() call rebuilds. Call this
    from any code path that writes a new file under VAULT_ROOT — otherwise
    wiki_search/wiki_backlinks may not see the new file for up to TTL_SEC."""
    global _VAULT_CACHE
    _VAULT_CACHE = None


def _resolve_note(path_or_name: str) -> Path | None:
    """Resolve 'projects/foo/bar' or 'bar' to an absolute .md file under the vault."""
    p = (path_or_name or "").strip()
    if not p:
        return None
    p_md = p if p.endswith(".md") else p + ".md"
    candidate = (VAULT_ROOT / p_md).resolve()
    try:
        candidate.relative_to(VAULT_ROOT.resolve())
    except ValueError:
        return None
    if candidate.exists():
        return candidate
    stem = Path(p).stem
    for md_path in VAULT_ROOT.rglob(f"{stem}.md"):
        resolved = md_path.resolve()
        try:
            resolved.relative_to(VAULT_ROOT.resolve())
        except ValueError:
            continue
        return resolved
    return None


async def _do_wiki_append(args: dict[str, Any]) -> str:
    """The actual write. Runs after user approves via approval gate.
    Returns a flat message string for the bot to send to the chat."""
    path_arg = (args.get("path") or "").strip()
    section = (args.get("section_heading") or "").strip()
    content = (args.get("content") or "").rstrip()

    abs_path = _resolve_note(path_arg)
    if not abs_path:
        target_rel = path_arg if path_arg.endswith(".md") else path_arg + ".md"
        candidate = (VAULT_ROOT / target_rel).resolve()
        try:
            candidate.relative_to(VAULT_ROOT.resolve())
        except ValueError:
            return f"wiki: refused — path {path_arg!r} resolves outside the vault."
        abs_path = candidate
        if not abs_path.parent.exists():
            return (
                f"wiki: can't write — parent dir {abs_path.parent} doesn't exist. "
                "create it manually first."
            )
        abs_path.touch()
        logger.info("wiki_append: created new note %s", abs_path)

    await _icloud_materialize(abs_path)
    try:
        post = frontmatter.load(str(abs_path))
    except Exception as e:  # noqa: BLE001
        return f"wiki: failed to parse {abs_path.name}: {e}"

    body = post.content
    if section:
        heading_re = re.compile(rf"^##\s+{re.escape(section)}\s*$", re.MULTILINE)
        m = heading_re.search(body)
        if m:
            after = body[m.end():]
            next_h2 = re.search(r"^##\s+", after, re.MULTILINE)
            if next_h2:
                insert_pos = m.end() + next_h2.start()
                new_body = body[:insert_pos].rstrip() + f"\n\n{content}\n\n" + body[insert_pos:]
            else:
                new_body = body.rstrip() + f"\n\n{content}\n"
        else:
            new_body = body.rstrip() + f"\n\n## {section}\n\n{content}\n"
    else:
        new_body = body.rstrip() + f"\n\n{content}\n"

    post.content = new_body
    try:
        text = frontmatter.dumps(post)
    except Exception as e:  # noqa: BLE001
        return f"wiki: dump failed: {e}"
    abs_path.write_text(text, encoding="utf-8")
    rel = abs_path.relative_to(VAULT_ROOT)
    section_str = f" under '## {section}'" if section else ""
    return f"wiki: appended {len(content)} chars to {rel}{section_str}."
