"""Semantic tool catalog with BM25 search.

Loaded from config/tools.yaml.  Each tool entry is enriched with semantic
metadata (domain, operation, description, examples, tags) either from
explicit fields the config-master wave may have added, or synthesised from
the tool id + server name.

The BM25 index document per tool is:
    name + description + examples (joined) + tags (joined) + domain + operation

Uses ``bm25s`` (already in deps).  Singleton via ``get_catalog()``.
Lazy load — importing this module does not build the index.

Usage:
    from tools.catalog import get_catalog
    results = get_catalog().search("email", k=5)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Semantic seed: domain/operation/description/tags per server or id-prefix.
# This covers the current yaml which has no explicit semantic metadata yet.
# When config-master adds explicit fields, _enrich() prefers those.
# ---------------------------------------------------------------------------

_SERVER_DOMAIN: dict[str, str] = {
    "hikari_memory": "memory",
    "hikari_utility": "utility",
    "hikari_wiki": "wiki",
    "hikari_dispatch": "dispatch",
    "hikari_router": "router",
    "google_workspace": "google",
    "notion": "notion",
    "github": "github",
    "apple_events": "apple",
    "playwright": "browser",
}

# Synonym / domain expansion boosts: query tokens → extra doc tokens.
# These are injected into the doc string so BM25 picks them up without
# custom scoring.
_DOMAIN_SYNONYMS: dict[str, list[str]] = {
    "email":    ["gmail", "message", "inbox", "send", "draft", "reply", "unread"],
    "gmail":    ["email", "message", "inbox", "unread"],
    "receipt":  ["log", "made", "moved", "learned", "avoided", "activity", "track"],
    "youtube":  ["video", "yt", "ytmusic", "music"],
    "weather":  ["temperature", "rain", "umbrella", "clothing"],
    "forecast": ["weather", "temperature", "rain"],
    "calendar": ["event", "schedule", "appointment", "meeting", "gcal", "apple"],
    "wiki":     ["notes", "knowledge", "obsidian", "page", "vault"],
    "github":   ["repo", "repository", "code", "issue", "pr", "pull request", "commit"],
    "notion":   ["page", "database", "workspace", "block"],
    "drive":    ["google drive", "file", "document", "folder", "upload"],
    "reminder": ["alert", "notification", "push", "telegram", "timer"],
}


# ---------------------------------------------------------------------------
# ToolEntry dataclass
# ---------------------------------------------------------------------------

@dataclass
class ToolEntry:
    """A single catalogued tool with semantic metadata."""
    name: str
    description: str
    domain: str
    operation: str
    risk_tier: str            # "safe" | "gated" | "destructive"
    credentials: list[str]
    examples: list[str]
    presentation_hint: str
    tags: list[str]
    bucket: int

    # Internal: the BM25 doc string for this entry (built once at index time).
    _doc: str = field(default="", repr=False, compare=False)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

class Catalog:
    """Semantic tool catalog backed by a BM25 index.

    Parameters
    ----------
    k1, b:
        BM25 tuning parameters (Robertson BM25 defaults).
    """

    def __init__(
        self,
        entries: list[ToolEntry],
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        self._entries = entries
        self._k1 = k1
        self._b = b
        self._bm25: Any = None  # lazy — built on first search()
        self._built = False

    @property
    def entries(self) -> list[ToolEntry]:
        return self._entries

    def _build_index(self) -> None:
        if self._built:
            return
        import bm25s
        docs = [e._doc for e in self._entries]
        if not docs:
            self._built = True
            return
        self._bm25 = bm25s.BM25(k1=self._k1, b=self._b)
        self._bm25.index(bm25s.tokenize(docs, stopwords="en"))
        self._built = True
        logger.debug("catalog: indexed %d tools", len(docs))

    def search(self, query: str, k: int = 5) -> list[ToolEntry]:
        """BM25 top-k search over the tool catalog.

        Returns up to k ToolEntry objects in descending relevance order.
        """
        import bm25s
        if not self._built:
            self._build_index()
        if self._bm25 is None or not self._entries:
            return []
        k = max(1, min(k, len(self._entries)))
        results = self._bm25.retrieve(
            bm25s.tokenize(query, stopwords="en"), k=k
        )
        out: list[ToolEntry] = []
        for row_idx in results.documents[0]:
            if row_idx < len(self._entries):
                out.append(self._entries[row_idx])
        return out


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def _token_expand(base_text: str) -> str:
    """Inject synonym expansions for known domain words."""
    extra: list[str] = []
    lower = base_text.lower()
    for keyword, synonyms in _DOMAIN_SYNONYMS.items():
        if keyword in lower:
            extra.extend(synonyms)
    if extra:
        return base_text + " " + " ".join(extra)
    return base_text


_ID_DOMAIN_OVERRIDES: dict[str, str] = {
    "mcp__hikari_utility__ytmusic": "music",
    "mcp__hikari_router__tool_search": "meta",
    "mcp__hikari_utility__weather": "weather",
    "mcp__hikari_utility__receipt": "receipt",
    "mcp__hikari_utility__reminder": "reminder",
    "mcp__hikari_utility__calendar": "calendar",
    "mcp__hikari_utility__decision": "decision",
    "mcp__hikari_utility__link": "link",
    "mcp__hikari_utility__note": "notes",
    "mcp__hikari_utility__arxiv": "research",
    "mcp__hikari_utility__places": "places",
    "mcp__hikari_utility__translate": "translation",
    "mcp__hikari_utility__currency": "finance",
    "mcp__hikari_utility__calc": "math",
    "mcp__hikari_utility__python": "code",
    "mcp__hikari_utility__skill": "skills",
}


def _infer_domain(tool_id: str, server: str | None) -> str:
    """Derive a domain label from id prefix overrides, then server, then id."""
    # Check fine-grained id-prefix overrides first
    best_prefix = ""
    best_domain = ""
    for prefix, dom in _ID_DOMAIN_OVERRIDES.items():
        if tool_id.startswith(prefix) and len(prefix) > len(best_prefix):
            best_prefix = prefix
            best_domain = dom
    if best_domain:
        return best_domain
    if server and server in _SERVER_DOMAIN:
        return _SERVER_DOMAIN[server]
    # Fallback: strip mcp prefix and grab service token
    m = re.match(r"mcp__([^_]+)", tool_id)
    if m:
        return m.group(1).replace("-", "_")
    return "utility"


def _infer_operation(tool_id: str) -> str:
    """Derive a coarse operation verb from the tool id."""
    lower = tool_id.lower()
    for verb in ("create", "delete", "update", "search", "list", "read",
                 "fetch", "send", "reply", "get", "query", "retrieve",
                 "append", "insert", "clear", "write", "run", "generate",
                 "save", "add", "remove", "cancel", "merge", "push", "fork",
                 "resolve", "capture", "dispatch", "print"):
        if verb in lower:
            return verb
    return "call"


def _infer_risk(gate: str | None, access_mode: str | None) -> str:
    if gate == "gatekeeper":
        return "gated"
    if access_mode == "destructive":
        return "destructive"
    if access_mode == "write":
        return "write"
    return "safe"


def _infer_credentials(server: str | None, scopes_provider: str | None) -> list[str]:
    if scopes_provider:
        return [scopes_provider]
    if server in {"google_workspace"}:
        return ["google"]
    if server in {"notion"}:
        return ["notion"]
    if server in {"github"}:
        return ["github"]
    return []


def _build_doc(entry: ToolEntry) -> str:
    """Construct the BM25 searchable document string for a tool entry."""
    parts = [
        entry.name,
        entry.description,
        " ".join(entry.examples),
        " ".join(entry.tags),
        entry.domain,
        entry.operation,
    ]
    raw = " ".join(p for p in parts if p)
    return _token_expand(raw)


def _entries_from_registry(path: Path | None = None) -> list[ToolEntry]:
    """Build ToolEntry list from tools.yaml via the existing ToolRegistry."""
    from tools._tools_yaml import DEFAULT_YAML_PATH, _load_yaml

    yaml_path = path or DEFAULT_YAML_PATH
    reg = _load_yaml(yaml_path)

    # Also load raw YAML for optional semantic metadata fields config-master may add.
    import yaml as _yaml
    raw_data = _yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    raw_tools_by_id: dict[str, dict] = {}
    for t in raw_data.get("tools") or []:
        raw_tools_by_id[str(t.get("id", ""))] = t

    entries: list[ToolEntry] = []
    for spec in reg.specs():
        tid = spec.id
        raw = raw_tools_by_id.get(tid, {})

        # tools.yaml is the single source of truth for description text
        # (validate_tool_registry.py enforces description: on every entry).
        # _synthesise_description is a loud last resort only.
        description = raw.get("description") or _synthesise_description(tid)
        domain = raw.get("domain") or _infer_domain(tid, spec.server)
        operation = raw.get("operation") or _infer_operation(tid)
        risk_tier = raw.get("risk_tier") or _infer_risk(spec.gate, spec.access_mode)
        credentials = raw.get("credentials") or _infer_credentials(
            spec.server, spec.scopes_provider
        )
        if isinstance(credentials, str):
            credentials = [credentials]

        # tools.yaml uses `example:` (singular str); accept `examples:` (list)
        # too. Merge with the synthesized NL asks — yaml examples are
        # code-usage strings, the NL defaults carry BM25 recall.
        raw_examples = raw.get("examples") or []
        if isinstance(raw_examples, str):
            raw_examples = [raw_examples]
        single = raw.get("example")
        if single and single not in raw_examples:
            raw_examples = [single, *raw_examples]
        examples = list(raw_examples) + _default_examples(tid, description)

        presentation_hint = raw.get("presentation_hint") or ""

        # Tags = explicit tags or derived from domain + operation
        raw_tags = raw.get("tags") or []
        if raw_tags:
            tags = list(raw_tags)
        else:
            tags = _derive_tags(tid, domain, operation, description)

        entry = ToolEntry(
            name=tid,
            description=description,
            domain=domain,
            operation=operation,
            risk_tier=risk_tier,
            credentials=credentials if isinstance(credentials, list) else [credentials],
            examples=examples,
            presentation_hint=presentation_hint,
            tags=tags,
            bucket=spec.bucket,
        )
        entry._doc = _build_doc(entry)
        entries.append(entry)

    return entries


def _synthesise_description(tool_id: str) -> str:
    """Last-resort: expand the tool id into a human-readable description.
    Should never fire — validate_tool_registry requires description: on
    every yaml entry. Loud so a regression is visible in logs."""
    logger.warning("catalog: no description for %s — synthesising from id", tool_id)
    stripped = re.sub(r"^mcp__[^_]+__", "", tool_id)
    tokens = re.split(r"[_\-]+", stripped)
    return " ".join(tokens)


def _default_examples(tool_id: str, description: str) -> list[str]:
    """Produce 1-2 natural-language example query fragments for BM25 training.

    Only applied to domain-specific tools — skipped for meta/utility tools that
    would otherwise pick up spurious domain keywords and pollute the BM25 index.
    """
    # Skip for meta/router tools — they already have a clear, unique description.
    _meta_prefixes = (
        "mcp__hikari_router__",
        "mcp__hikari_dispatch__",
    )
    if any(tool_id.startswith(p) for p in _meta_prefixes):
        return []
    # Generic examples derived from domain keywords in description
    lower = description.lower()
    exs: list[str] = []
    if "email" in lower or "gmail" in lower:
        exs.append("check my emails")
    if "calendar" in lower or "event" in lower or "meeting" in lower:
        exs.append("what's on my calendar")
    if "receipt" in lower or "activity" in lower or "track" in lower:
        exs.append("log what I did today")
    if "weather" in lower or "temperature" in lower:
        exs.append("what's the weather")
    if "wiki" in lower or "knowledge" in lower or "vault" in lower:
        exs.append("what does my wiki say about")
    if "github" in lower or "repository" in lower or "pull request" in lower:
        exs.append("check github issues")
    if "notion" in lower and ("page" in lower or "database" in lower or "block" in lower):
        exs.append("look up notion page")
    return exs[:2]


def _derive_tags(tool_id: str, domain: str, operation: str, description: str) -> list[str]:
    """Derive a tag list for BM25 doc enrichment."""
    tags = [domain, operation]
    lower = (tool_id + " " + description).lower()
    # Service-specific tags
    for kw in ("gmail", "calendar", "drive", "docs", "sheets", "slides",
               "notion", "github", "youtube", "weather", "wiki", "reminder",
               "receipt", "memory", "photo", "translate", "currency",
               "arxiv", "places", "ytmusic", "notes", "sql",
               "playwright", "browser", "dispatch", "skill",
               "decision", "link", "attachment", "python"):
        if kw in lower:
            tags.append(kw)
    # Access-pattern tags
    if "read" in lower or "list" in lower or "get" in lower or "fetch" in lower:
        tags.append("read")
    if "write" in lower or "create" in lower or "update" in lower or "send" in lower:
        tags.append("write")
    return list(dict.fromkeys(tags))  # stable dedup


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_CATALOG: Catalog | None = None


def get_catalog(path: Path | None = None) -> Catalog:
    """Return the module-level singleton Catalog, building it on first call."""
    global _CATALOG
    if _CATALOG is None:
        entries = _entries_from_registry(path)
        _CATALOG = Catalog(entries)
        logger.info("catalog: loaded %d tool entries", len(entries))
    return _CATALOG


def _reset_catalog() -> None:
    """For testing only — clear the singleton so the next call rebuilds."""
    global _CATALOG
    _CATALOG = None


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    queries = [
        "email",
        "receipt",
        "youtube",
        "weather",
        "calendar",
        "wiki",
        "github",
        "notion",
    ]

    catalog = get_catalog()
    print(f"Catalog loaded: {len(catalog.entries)} tools indexed\n")

    failures: list[str] = []

    for q in queries:
        results = catalog.search(q, k=3)
        print(f"--- query: {q!r} ---")
        for i, entry in enumerate(results, 1):
            print(f"  {i}. {entry.name}")
            print(f"     domain={entry.domain!r}  op={entry.operation!r}  tags={entry.tags[:4]}")
        print()

        # Basic sanity: for each query at least one result should mention the query word
        hit = any(
            q in (e.name + e.description + " ".join(e.tags)).lower()
            for e in results
        )
        if not hit:
            failures.append(f"WARN: query {q!r} → no result mentions the query term")

    if failures:
        for f in failures:
            print(f, file=sys.stderr)
        sys.exit(1)
    else:
        print("All queries returned relevant results.")
