"""Self-contained schema + CRUD for the link shelf.

This module owns its tables. The first call to ``_ensure_schema`` runs
``CREATE TABLE IF NOT EXISTS`` against the shared SQLite DB, so the
feature folder is fully self-contained — no edits to ``storage/db.py``
are required to add a feature.

Two tables:
  - ``links``: the actual shelf entries.
  - ``link_fts``: FTS5 mirror over title + snippet + tags + note for
    keyword search. Mirrored manually on insert/update/delete (no
    triggers — keeps the migration simple and matches the rest of the
    DB's pattern).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from typing import Any

from agents import config as cfg
from storage.db import _conn

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    title TEXT,
    snippet TEXT,
    kind TEXT NOT NULL DEFAULT 'later'
        CHECK (kind IN ('later', 'useful', 'source', 'inspiration')),
    tags_json TEXT NOT NULL DEFAULT '[]',
    note TEXT,
    added_at TEXT NOT NULL,
    last_recalled_at TEXT,
    recall_count INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_links_kind ON links(kind);
CREATE INDEX IF NOT EXISTS idx_links_added ON links(added_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_links_url ON links(url) WHERE archived = 0;

-- FTS table is content-owning (no ``content=''`` clause). We manually
-- INSERT / DELETE / re-INSERT on writes — simpler than contentless
-- tables which require ``INSERT INTO link_fts(link_fts, rowid) VALUES
-- ('delete', ?)`` to remove rows. Storage cost is tiny.
CREATE VIRTUAL TABLE IF NOT EXISTS link_fts USING fts5(
    title,
    snippet,
    tags,
    note,
    tokenize='unicode61'
);
"""


_VALID_KINDS = set(
    cfg.get("link_shelf.valid_kinds")
    or ["later", "useful", "source", "inspiration"]
)


# Process-level sentinel so the schema bootstrap only runs on the first
# touch. Matches the pattern in storage/db.py — the central conn helper
# already runs its own migrations, so we just need ours.
_SCHEMA_INITIALIZED = False
_SCHEMA_LOCK = threading.Lock()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    global _SCHEMA_INITIALIZED
    if _SCHEMA_INITIALIZED:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_INITIALIZED:
            return
        # All-or-nothing: if any DDL fails partway, the sentinel stays False
        # and SQLite rolls the rest back, so the next call retries cleanly.
        with conn:
            for stmt in _SCHEMA.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    conn.execute(stmt)
        _SCHEMA_INITIALIZED = True


def _reset_schema_sentinel() -> None:
    """Test helper — re-bootstrap schema on next access."""
    global _SCHEMA_INITIALIZED
    _SCHEMA_INITIALIZED = False


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    try:
        d["tags"] = json.loads(d.pop("tags_json") or "[]")
    except json.JSONDecodeError:
        d["tags"] = []
    return d


def _normalize_kind(kind: str | None, default: str = "later") -> str:
    if not kind:
        return default
    k = kind.strip().lower()
    return k if k in _VALID_KINDS else default


def _normalize_tags(tags: Any) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        # Be forgiving — accept comma-separated strings too.
        return [t.strip() for t in tags.split(",") if t.strip()]
    if isinstance(tags, list):
        return [str(t).strip() for t in tags if str(t).strip()]
    return []


def _fts_text(
    title: str | None, snippet: str | None, tags: list[str], note: str | None
) -> tuple[str, str, str, str]:
    return (
        title or "",
        snippet or "",
        " ".join(tags),
        note or "",
    )


# ---------- CRUD ----------


def insert(
    *,
    url: str,
    title: str | None,
    snippet: str | None,
    kind: str,
    tags: list[str],
    note: str | None,
) -> int:
    kind_n = _normalize_kind(kind)
    tags_n = _normalize_tags(tags)
    now = _now()
    with _conn() as c:
        _ensure_schema(c)
        # If the URL is already on the active shelf, treat this as an
        # update rather than a unique-constraint failure.
        existing = c.execute(
            "SELECT id FROM links WHERE url = ? AND archived = 0",
            (url,),
        ).fetchone()
        if existing:
            link_id = existing["id"]
            c.execute(
                "UPDATE links SET title = COALESCE(?, title), "
                "snippet = COALESCE(?, snippet), kind = ?, tags_json = ?, "
                "note = COALESCE(?, note) WHERE id = ?",
                (title, snippet, kind_n, json.dumps(tags_n), note, link_id),
            )
            t, s, tg, nt = _fts_text(title, snippet, tags_n, note)
            c.execute("DELETE FROM link_fts WHERE rowid = ?", (link_id,))
            c.execute(
                "INSERT INTO link_fts(rowid, title, snippet, tags, note) "
                "VALUES (?, ?, ?, ?, ?)",
                (link_id, t, s, tg, nt),
            )
            return link_id
        cur = c.execute(
            "INSERT INTO links(url, title, snippet, kind, tags_json, note, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (url, title, snippet, kind_n, json.dumps(tags_n), note, now),
        )
        link_id = int(cur.lastrowid)
        t, s, tg, nt = _fts_text(title, snippet, tags_n, note)
        c.execute(
            "INSERT INTO link_fts(rowid, title, snippet, tags, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (link_id, t, s, tg, nt),
        )
        return link_id


def search(
    *,
    query: str,
    kind: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    if limit <= 0:
        limit = 10
    with _conn() as c:
        _ensure_schema(c)
        q = query.strip()
        if not q:
            return []
        # FTS5 'NEAR' is overkill; default match handles word stems.
        # Escape double-quotes by doubling them, then wrap as a phrase
        # so spaces inside the query don't break the parser.
        escaped = q.replace('"', '""')
        match_expr = f'"{escaped}"'
        sql = (
            "SELECT l.* FROM links l "
            "JOIN link_fts f ON f.rowid = l.id "
            "WHERE l.archived = 0 AND link_fts MATCH ? "
        )
        params: list[Any] = [match_expr]
        if kind:
            sql += "AND l.kind = ? "
            params.append(_normalize_kind(kind))
        sql += "ORDER BY rank LIMIT ?"
        params.append(limit)
        try:
            rows = c.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            # FTS5 can reject some queries (e.g. starting with operators).
            # Fall back to a LIKE scan rather than failing the call.
            # Escape backslash first, then SQL LIKE wildcards (`%` and `_`)
            # so a query like "50%" doesn't match every row. Pair the
            # escape with an ESCAPE clause on each LIKE site.
            logger.debug("link_fts MATCH rejected query %r: %s; falling back to LIKE", q, exc)
            escaped_like = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{escaped_like}%"
            sql_like = (
                "SELECT * FROM links WHERE archived = 0 AND "
                "(title LIKE ? ESCAPE '\\' OR snippet LIKE ? ESCAPE '\\' OR "
                "tags_json LIKE ? ESCAPE '\\' OR note LIKE ? ESCAPE '\\' OR "
                "url LIKE ? ESCAPE '\\') "
            )
            like_params: list[Any] = [like, like, like, like, like]
            if kind:
                sql_like += "AND kind = ? "
                like_params.append(_normalize_kind(kind))
            sql_like += "ORDER BY added_at DESC LIMIT ?"
            like_params.append(limit)
            rows = c.execute(sql_like, like_params).fetchall()
        return [_row_to_dict(r) for r in rows]


def list_links(
    *,
    kind: str | None = None,
    tag: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    if limit <= 0:
        limit = 20
    with _conn() as c:
        _ensure_schema(c)
        sql = "SELECT * FROM links WHERE archived = 0 "
        params: list[Any] = []
        if kind:
            sql += "AND kind = ? "
            params.append(_normalize_kind(kind))
        if tag:
            sql += "AND tags_json LIKE ? "
            params.append(f'%"{tag.strip()}"%')
        sql += "ORDER BY added_at DESC LIMIT ?"
        params.append(limit)
        rows = c.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]


def update(
    *,
    link_id: int,
    kind: str | None = None,
    tags: list[str] | None = None,
    note: str | None = None,
) -> dict[str, Any] | None:
    with _conn() as c:
        _ensure_schema(c)
        row = c.execute("SELECT * FROM links WHERE id = ? AND archived = 0",
                        (link_id,)).fetchone()
        if not row:
            return None
        new_kind = _normalize_kind(kind, default=row["kind"]) if kind else row["kind"]
        if tags is not None:
            new_tags = _normalize_tags(tags)
        else:
            new_tags = json.loads(row["tags_json"] or "[]")
        new_note = note if note is not None else row["note"]
        c.execute(
            "UPDATE links SET kind = ?, tags_json = ?, note = ? WHERE id = ?",
            (new_kind, json.dumps(new_tags), new_note, link_id),
        )
        t, s, tg, nt = _fts_text(row["title"], row["snippet"], new_tags, new_note)
        c.execute("DELETE FROM link_fts WHERE rowid = ?", (link_id,))
        c.execute(
            "INSERT INTO link_fts(rowid, title, snippet, tags, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (link_id, t, s, tg, nt),
        )
        updated = c.execute("SELECT * FROM links WHERE id = ?",
                            (link_id,)).fetchone()
        return _row_to_dict(updated)


def delete(*, link_id: int) -> bool:
    """Permanently remove an active (non-archived) link.

    Matches the semantics of ``update`` and ``get``: archived rows are
    invisible to the shelf API. Callers that need to scrub archived rows
    should go through a dedicated admin path (none exists today)."""
    with _conn() as c:
        _ensure_schema(c)
        row = c.execute("SELECT id FROM links WHERE id = ? AND archived = 0",
                        (link_id,)).fetchone()
        if not row:
            return False
        c.execute("DELETE FROM link_fts WHERE rowid = ?", (link_id,))
        c.execute("DELETE FROM links WHERE id = ?", (link_id,))
        return True


def mark_recalled(*, link_id: int) -> None:
    """Bump recall_count + last_recalled_at so we can later show 'links
    you've come back to' or de-prioritize never-touched stale entries."""
    with _conn() as c:
        _ensure_schema(c)
        c.execute(
            "UPDATE links SET recall_count = recall_count + 1, "
            "last_recalled_at = ? WHERE id = ?",
            (_now(), link_id),
        )


def get(*, link_id: int) -> dict[str, Any] | None:
    with _conn() as c:
        _ensure_schema(c)
        row = c.execute("SELECT * FROM links WHERE id = ?",
                        (link_id,)).fetchone()
        return _row_to_dict(row) if row else None
