"""Obsidian wiki tools — filesystem-direct read/write of the user's personal knowledge graph.

The vault lives in iCloud Drive, so reads must materialize files via `brctl download`
before touching them. Writes use python-frontmatter + ruamel.yaml to preserve key order
and avoid churn on every save.

Graph queries (backlinks, wikilink resolution) go through obsidiantools' networkx graph,
which is built once at module load and cached via lru_cache. Long-term we may rebuild
on watchdog FS events; for now, restart-on-change is acceptable.
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
from claude_agent_sdk import tool
from obsidiantools.api import Vault
from ruamel.yaml import YAML

from storage import db

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


def _ok(text: str, data: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if data is not None:
        body["data"] = data
    return body


@tool(
    "wiki_search",
    "Search the user's Obsidian wiki by query. Matches against note filenames (fuzzy) "
    "and full-text content. Returns top matches with paths and short excerpts. "
    "Use to find notes on a topic before reading them.",
    {"query": str, "limit": int},
)
async def wiki_search(args: dict[str, Any]) -> dict[str, Any]:
    query = (args.get("query") or "").strip()
    limit = max(1, min(20, int(args.get("limit") or 5)))
    if not query:
        return _ok("wiki_search: empty query.")

    q_lower = query.lower()
    q_tokens = [t for t in re.findall(r"\w+", q_lower) if len(t) > 2]

    hits: list[tuple[float, str, str]] = []  # (score, rel_path, excerpt)
    v = _vault()
    for note_name, rel_path in v.md_file_index.items():
        name_score = 0.0
        name_lower = note_name.lower()
        if q_lower in name_lower:
            name_score = 3.0
        elif any(t in name_lower for t in q_tokens):
            name_score = 1.5

        content_score = 0.0
        excerpt = ""
        try:
            text = v.get_readable_text(note_name) or ""
        except Exception:  # noqa: BLE001
            text = ""
        text_lower = text.lower()
        if q_lower in text_lower:
            content_score = 2.0
            idx = text_lower.find(q_lower)
            start = max(0, idx - 60)
            end = min(len(text), idx + len(q_lower) + 60)
            excerpt = text[start:end].replace("\n", " ")
        elif q_tokens:
            matched = sum(1 for t in q_tokens if t in text_lower)
            if matched:
                content_score = 0.5 * matched

        total = name_score + content_score
        if total > 0:
            hits.append((total, str(rel_path), excerpt[:200]))

    hits.sort(key=lambda x: -x[0])
    hits = hits[:limit]
    if not hits:
        return _ok(f"wiki_search: no matches for {query!r}.")

    lines = [f"top {len(hits)} wiki matches for {query!r}:"]
    for score, path, excerpt in hits:
        lines.append(f"  [{score:.1f}] {path}" + (f" — {excerpt}" if excerpt else ""))
    return _ok(
        "\n".join(lines),
        data=[{"score": s, "path": p, "excerpt": e} for s, p, e in hits],
    )


@tool(
    "wiki_read",
    "Read a note from the user's Obsidian wiki by relative path (e.g. 'projects/meria/meria') "
    "or bare note name. Returns frontmatter + body. Materializes from iCloud if needed.",
    {"path": str},
)
async def wiki_read(args: dict[str, Any]) -> dict[str, Any]:
    from agents.injection_guard import wrap_untrusted

    path_arg = (args.get("path") or "").strip()
    abs_path = _resolve_note(path_arg)
    if not abs_path:
        return _ok(f"wiki_read: note not found: {path_arg!r}.")
    _icloud_materialize(abs_path)
    try:
        post = frontmatter.load(str(abs_path))
    except Exception as e:  # noqa: BLE001
        return _ok(f"wiki_read: failed to parse {abs_path.name}: {e}")

    rel = abs_path.relative_to(VAULT_ROOT)
    metadata_str = "\n".join(f"  {k}: {v!r}" for k, v in post.metadata.items()) or "  (none)"
    body = post.content
    # Wrap the body — wiki content may contain attacker-injected text via
    # synced notes, web-clipped pages, or LLM-generated entries from prior runs.
    wrapped_body = wrap_untrusted("mcp__hikari_wiki__wiki_read", body)
    return _ok(
        f"# {rel}\n\nfrontmatter:\n{metadata_str}\n\nbody:\n{wrapped_body}",
        data={
            "rel_path": str(rel), "metadata": dict(post.metadata),
            "body": body, "body_len": len(body),
            "untrusted": True,
        },
    )


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


@tool(
    "wiki_append",
    "Append content to a note in the user's Obsidian wiki. If section_heading is given, "
    "append under that H2 (creating it if absent). Frontmatter is preserved verbatim. "
    "Use [[wikilinks]] for cross-references. The note path is relative to the vault root. "
    "Phase 8: this tool runs without an approval prompt. The wiki is reversible "
    "(iCloud version history) and every write is audit-logged.",
    {"path": str, "section_heading": str, "content": str},
)
async def wiki_append(args: dict[str, Any]) -> dict[str, Any]:
    path_arg = (args.get("path") or "").strip()
    section = (args.get("section_heading") or "").strip()
    content = (args.get("content") or "").rstrip()
    if not path_arg:
        return _ok("wiki_append: path is required.")
    if not content:
        return _ok("wiki_append: content is empty.")

    result_str = await _do_wiki_append(args)
    # Audit every wiki append so the trail is intact even without an approval row.
    try:
        section_str = f" under '## {section}'" if section else ""
        db.audit_append(
            tool="mcp__hikari_wiki__wiki_append",
            args_json_redacted=(
                f"path={path_arg!r}{section_str} ({len(content)} chars)"
            )[:500],
            result_summary=result_str[:500],
            approved_by="auto",
        )
    except Exception:
        logger.exception("wiki_append: audit_append failed (non-fatal)")
    return _ok(result_str, data={"path": path_arg})


@tool(
    "wiki_backlinks",
    "List notes in the user's wiki that link to a given topic/note. "
    "Useful for finding cross-references and related material. "
    "topic can be a note name or a topic substring.",
    {"topic": str, "limit": int},
)
async def wiki_backlinks(args: dict[str, Any]) -> dict[str, Any]:
    topic = (args.get("topic") or "").strip()
    limit = max(1, min(50, int(args.get("limit") or 10)))
    if not topic:
        return _ok("wiki_backlinks: topic is required.")

    v = _vault()
    # Try exact note-name match first
    if topic in v.md_file_index:
        try:
            links = v.get_backlinks(topic)
        except Exception:  # noqa: BLE001
            links = []
        if links:
            shown = links[:limit]
            lines = [f"{len(links)} backlinks to {topic!r}:"]
            lines.extend(f"  - {n}" for n in shown)
            return _ok("\n".join(lines), data={"topic": topic, "backlinks": links})

    # Fall back: find notes whose name contains the topic, return their combined backlinks
    matching_notes = [n for n in v.md_file_index if topic.lower() in n.lower()]
    if not matching_notes:
        return _ok(f"wiki_backlinks: no notes match {topic!r}.")

    aggregated: dict[str, int] = {}
    for n in matching_notes[:5]:
        try:
            for src in v.get_backlinks(n):
                aggregated[src] = aggregated.get(src, 0) + 1
        except Exception:  # noqa: BLE001
            continue
    if not aggregated:
        return _ok(f"wiki_backlinks: matched {len(matching_notes)} note(s) but no backlinks.")

    ranked = sorted(aggregated.items(), key=lambda kv: -kv[1])[:limit]
    lines = [f"backlinks via fuzzy match on {topic!r} ({len(matching_notes)} notes):"]
    lines.extend(f"  - {src} (×{count})" for src, count in ranked)
    return _ok(
        "\n".join(lines),
        data={"topic": topic, "matched_notes": matching_notes, "backlinks": dict(ranked)},
    )


# Public tools — registered on the always-on `hikari_wiki` MCP server. These
# are the tools Sonnet can see on every turn (subject to allowlist). Phase 8
# dropped the `wiki_append_confirmed` sibling because `wiki_append` no longer
# requires approval.
PUBLIC_TOOLS = [wiki_search, wiki_read, wiki_append, wiki_backlinks]

# Phase 8: no privileged wiki tools. CONFIRMED_TOOLS retained for back-compat
# (empty list) so any importer using the symbol doesn't break.
CONFIRMED_TOOLS: list = []

# Backwards-compat alias — some imports may reference the flat list.
ALL_TOOLS = PUBLIC_TOOLS
