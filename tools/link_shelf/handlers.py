"""Link-shelf tool handlers — invoked lazily on first call.

Heavy imports (``httpx`` for title fetch) live inside the functions so
the manifest in ``__init__.py`` stays cheap. The ``tools._lazy`` stubs
only load this module when one of the five tools is actually called.

Sprint 6E: metadata fetches go through ``_safe_fetch.safe_fetch`` which
pre-resolves the host and refuses private / link-local / cloud-metadata
addresses, validates each redirect hop, and caps the chain at 3.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

from agents import config as cfg
from agents.injection_guard import wrap_untrusted
from tools._response import ok as _ok
from tools.link_shelf import db as shelf_db
from tools.link_shelf._safe_fetch import SafeFetchError, safe_fetch

logger = logging.getLogger(__name__)


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_META_DESC_RE = re.compile(
    r'<meta\s+[^>]*?name=["\']description["\'][^>]*?content=["\'](.*?)["\']',
    re.IGNORECASE | re.DOTALL,
)
_META_OG_DESC_RE = re.compile(
    r'<meta\s+[^>]*?property=["\']og:description["\'][^>]*?content=["\'](.*?)["\']',
    re.IGNORECASE | re.DOTALL,
)

_FETCH_TIMEOUT_SEC = cfg.get("link_shelf.fetch_timeout_sec") or 5.0
# plenty for <head>, refuses huge pages
_FETCH_MAX_BYTES = cfg.get("link_shelf.fetch_max_bytes") or 200_000
_USER_AGENT = cfg.get("link_shelf.user_agent") \
    or "hikari-agent link-shelf/1.0 (+https://github.com/hikari-agent)"


def _looks_like_url(s: str) -> bool:
    try:
        p = urlparse(s)
    except ValueError:
        return False
    return p.scheme in {"http", "https"} and bool(p.netloc)


async def _fetch_metadata(url: str) -> tuple[str | None, str | None]:
    """Pull (title, description) from a URL. Best-effort — any failure
    returns (URL-derived title, None) and we save the link without metadata.

    Sprint 6E: delegates to ``safe_fetch`` which refuses SSRF targets
    (loopback / private / link-local / cloud-metadata) and bounds the
    redirect chain. Returns RAW extracted text — wrapping with
    ``wrap_untrusted`` happens at READ time (search / list_links) so the
    DB and FTS index stay clean.
    """
    try:
        raw_bytes, ctype, _final_url = await safe_fetch(
            url,
            timeout_sec=_FETCH_TIMEOUT_SEC,
            max_bytes=_FETCH_MAX_BYTES,
            user_agent=_USER_AGENT,
        )
    except SafeFetchError as exc:
        logger.info("link_shelf safe_fetch refused %s: %s", url, exc)
        return (_url_to_title(url), None)
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.debug("link_shelf metadata fetch failed for %s: %s", url, exc)
        return (_url_to_title(url), None)

    if "html" not in ctype.lower() and "text" not in ctype.lower():
        # Non-HTML (PDF, image, etc.). Fall back to URL-derived title.
        return (_url_to_title(url), None)
    body = raw_bytes.decode("utf-8", errors="replace")
    title = _extract(body, _TITLE_RE)
    desc = _extract(body, _META_OG_DESC_RE) or _extract(body, _META_DESC_RE)
    return (title or _url_to_title(url), desc)


def _wrap_title(title: str | None) -> str | None:
    """Wrap a fetched page title for LLM-facing surfaces."""
    if not title:
        return title
    return wrap_untrusted("link_shelf", title)


def _safe_link_payload(row: dict) -> dict:
    """Return a shallow copy of `row` with title/snippet wrapped for LLM
    consumption. Used to clean both the text rendering AND the data dict
    so the model never sees raw fetched page content unwrapped."""
    safe = dict(row)
    if safe.get("title"):
        safe["title"] = wrap_untrusted("link_shelf", safe["title"])
    if safe.get("snippet"):
        safe["snippet"] = wrap_untrusted("link_shelf", safe["snippet"])
    return safe


def _extract(body: str, pattern: re.Pattern[str]) -> str | None:
    m = pattern.search(body)
    if not m:
        return None
    raw = m.group(1).strip()
    # Crude entity unescape — good enough for &amp; / &quot; / &#39;.
    raw = (raw.replace("&amp;", "&").replace("&quot;", '"')
              .replace("&#39;", "'").replace("&apos;", "'")
              .replace("&lt;", "<").replace("&gt;", ">"))
    # Collapse whitespace.
    raw = re.sub(r"\s+", " ", raw)
    return raw[:400] or None


def _url_to_title(url: str) -> str:
    """Derive a fallback title from the URL host + path."""
    try:
        p = urlparse(url)
    except ValueError:
        return url
    host = p.netloc.removeprefix("www.")
    tail = p.path.rstrip("/").rsplit("/", 1)[-1] if p.path else ""
    if tail:
        # turn "some-article-title" into "Some Article Title"
        tail = tail.replace("-", " ").replace("_", " ").strip()
        return f"{host} — {tail}"[:200]
    return host[:200]


def _fmt_tags(tags: list[str]) -> str:
    return ", ".join(tags) if tags else "—"


# ---------- public tool handlers ----------


async def save(args: dict[str, Any]) -> dict[str, Any]:
    url = (args.get("url") or "").strip()
    if not url or not _looks_like_url(url):
        return _ok("refused: link_save needs a valid http(s) URL")
    kind = (args.get("kind") or "later").strip().lower()
    raw_tags = args.get("tags") or []
    note = (args.get("note") or "").strip() or None

    # Normalize once here so we can quote the same shape in the response
    # without a second DB round-trip (which could AttributeError on None).
    normalized_tags = shelf_db._normalize_tags(raw_tags)
    title, snippet = await _fetch_metadata(url)
    # Raw title persists to DB / FTS — keep it clean for search.
    link_id = shelf_db.insert(
        url=url, title=title, snippet=snippet,
        kind=kind, tags=normalized_tags, note=note,
    )
    # Wrap title in the LLM-facing response — same content, marked as
    # untrusted to neutralise any prompt-injection sitting in the page title.
    safe_title = _wrap_title(title) or url
    summary = (
        f"saved [{kind}] #{link_id}: {safe_title}"
        f" (tags: {_fmt_tags(normalized_tags)})"
    )
    # `data` is JSON-serialized into the LLM-facing text body by _ok,
    # so the title here must also be wrapped — otherwise a malicious
    # page title leaks unwrapped on the save turn.
    return _ok(summary, data={
        "id": link_id, "url": url, "title": _wrap_title(title), "kind": kind,
        "tags": normalized_tags,
    })


async def search(args: dict[str, Any]) -> dict[str, Any]:
    query = (args.get("query") or "").strip()
    if not query:
        return _ok("refused: link_search needs a query")
    kind = (args.get("kind") or "").strip().lower() or None
    limit = int(args.get("limit") or cfg.get("link_shelf.search_default_limit") or 10)

    hits = shelf_db.search(query=query, kind=kind, limit=limit)
    if not hits:
        return _ok(f"no links matched {query!r}", data={"hits": []})
    for h in hits:
        # Bump recall counter so we can de-stale-ify later.
        shelf_db.mark_recalled(link_id=h["id"])
    safe_hits = [_safe_link_payload(h) for h in hits]
    lines = [f"found {len(hits)} link(s) for {query!r}:"]
    for h in safe_hits:
        tags = _fmt_tags(h["tags"])
        lines.append(
            f"  - #{h['id']} [{h['kind']}] {h.get('title') or h['url']}"
            f"\n    {h['url']}\n    tags: {tags}"
        )
    return _ok("\n".join(lines), data={"hits": safe_hits})


async def list_links(args: dict[str, Any]) -> dict[str, Any]:
    kind = (args.get("kind") or "").strip().lower() or None
    tag = (args.get("tag") or "").strip() or None
    limit = int(args.get("limit") or cfg.get("link_shelf.list_default_limit") or 20)
    rows = shelf_db.list_links(kind=kind, tag=tag, limit=limit)
    if not rows:
        filt = []
        if kind:
            filt.append(f"kind={kind}")
        if tag:
            filt.append(f"tag={tag}")
        suffix = f" ({', '.join(filt)})" if filt else ""
        return _ok(f"link shelf is empty{suffix}", data={"links": []})
    safe_rows = [_safe_link_payload(r) for r in rows]
    lines = [f"link shelf ({len(rows)} link{'s' if len(rows) != 1 else ''}):"]
    for r in safe_rows:
        tags = _fmt_tags(r["tags"])
        lines.append(
            f"  - #{r['id']} [{r['kind']}] {r.get('title') or r['url']}"
            f"\n    {r['url']}\n    tags: {tags}"
        )
    return _ok("\n".join(lines), data={"links": safe_rows})


async def update(args: dict[str, Any]) -> dict[str, Any]:
    raw_id = args.get("id")
    if raw_id is None:
        return _ok("refused: link_update needs an id")
    try:
        link_id = int(raw_id)
    except (TypeError, ValueError):
        return _ok(f"refused: invalid id {raw_id!r}")
    kind = args.get("kind")
    tags = args.get("tags")
    note = args.get("note")
    if kind is None and tags is None and note is None:
        return _ok("refused: nothing to update (pass kind / tags / note)")
    updated = shelf_db.update(
        link_id=link_id,
        kind=kind, tags=tags, note=note,
    )
    if not updated:
        return _ok(f"no link with id={link_id}")
    safe_updated = _safe_link_payload(updated)
    tag_str = _fmt_tags(safe_updated["tags"])
    return _ok(
        f"updated #{link_id} [{safe_updated['kind']}] tags: {tag_str}",
        data={"link": safe_updated},
    )


async def delete(args: dict[str, Any]) -> dict[str, Any]:
    raw_id = args.get("id")
    if raw_id is None:
        return _ok("refused: link_delete needs an id")
    try:
        link_id = int(raw_id)
    except (TypeError, ValueError):
        return _ok(f"refused: invalid id {raw_id!r}")
    if shelf_db.delete(link_id=link_id):
        return _ok(f"deleted #{link_id}", data={"id": link_id})
    return _ok(f"no link with id={link_id}")
