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

import logging
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

import frontmatter
from obsidiantools.api import Vault
from ruamel.yaml import YAML

logger = logging.getLogger(__name__)

VAULT_ROOT = Path(
    os.environ.get("HIKARI_WIKI_VAULT")
    or Path.home() / "Library/Mobile Documents/iCloud~md~obsidian/Documents/alt-wiki"
).expanduser()

_yaml = YAML(typ="rt")
_yaml.preserve_quotes = True
_yaml.indent(mapping=2, sequence=4, offset=2)


def _icloud_materialize(path: Path, timeout: int = 30) -> None:
    """Force iCloud to download a placeholder file. No-op if already materialized."""
    if not path.exists() and not path.with_name(f".{path.name}.icloud").exists():
        return
    try:
        subprocess.run(
            ["brctl", "download", str(path)],
            check=False, timeout=timeout, capture_output=True,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("brctl download failed for %s: %s", path, e)


@lru_cache(maxsize=1)
def _vault() -> Vault:
    """Build the vault graph once. Connected + gathered so all queries work."""
    logger.info("connecting to obsidian vault at %s", VAULT_ROOT)
    v = Vault(VAULT_ROOT).connect().gather()
    logger.info("vault ready: %d notes, %d backlinks", len(v.md_file_index), sum(
        1 for _ in v.backlinks_index.values()
    ))
    return v


def _resolve_note(path_or_name: str) -> Path | None:
    """Resolve 'projects/foo/bar' or 'bar' to an absolute .md file under the vault."""
    p = (path_or_name or "").strip()
    if not p:
        return None
    if not p.endswith(".md"):
        p_md = p + ".md"
    else:
        p_md = p
    candidate = VAULT_ROOT / p_md
    if candidate.exists():
        return candidate
    # Try by filename across the vault
    stem = Path(p).stem
    for md_path in VAULT_ROOT.rglob(f"{stem}.md"):
        return md_path
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
        abs_path = VAULT_ROOT / target_rel
        if not abs_path.parent.exists():
            return (
                f"wiki: can't write — parent dir {abs_path.parent} doesn't exist. "
                "create it manually first."
            )
        abs_path.touch()
        logger.info("wiki_append: created new note %s", abs_path)

    _icloud_materialize(abs_path)
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
