"""Self-contained schema + CRUD for day_receipt.

Owns its own SQLite file (default ``~/.day-receipt/receipt.db``,
override via ``DAY_RECEIPT_DB``) — NOT the shared ``hikari.db``. The
standalone CLI at ``/Users/alt/work_dir/apps/day-receipt`` and these
in-process tools both resolve the same default path so they share data
on the user's main device.

Two tables: ``entries`` (one row per logged thing in a band) and
``notes`` (one row per dated free-form note). Schema bootstrap follows
the ``tools/link_shelf/db.py`` pattern — process-level
``_SCHEMA_INITIALIZED`` sentinel + lock so the ``CREATE TABLE IF NOT
EXISTS`` only runs on the first touch.

``models.py`` from the standalone repo is merged in here (Entry,
Receipt, DaySummary dataclasses + CATEGORIES) — kept frozen so the
public shape is unambiguous.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from tools.day_receipt._shared import CATEGORIES, Category, db_path

# ---------- dataclasses (merged from models.py) ----------


@dataclass(frozen=True)
class Entry:
    id: int
    receipt_date: date
    category: Category
    text: str
    created_at: datetime
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Receipt:
    receipt_date: date
    entries: tuple[Entry, ...]
    note: str | None = None

    def by_category(self, category: Category) -> tuple[Entry, ...]:
        return tuple(e for e in self.entries if e.category == category)

    @property
    def counts(self) -> dict[Category, int]:
        out: dict[Category, int] = {c: 0 for c in CATEGORIES}
        for e in self.entries:
            out[e.category] += 1
        return out


@dataclass(frozen=True)
class DaySummary:
    receipt_date: date
    counts: dict[Category, int] = field(default_factory=dict)
    has_note: bool = False


# ---------- schema + bootstrap ----------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_date TEXT    NOT NULL,
    category     TEXT    NOT NULL CHECK (category IN ('made','moved','learned','avoided')),
    text         TEXT    NOT NULL,
    tags         TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_date ON entries(receipt_date);
CREATE INDEX IF NOT EXISTS idx_entries_category ON entries(category);

CREATE TABLE IF NOT EXISTS notes (
    receipt_date TEXT PRIMARY KEY,
    note         TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
"""

# Process-level sentinel. Matches ``tools/link_shelf/db.py``: skip
# the DDL on every subsequent connection but always run it on the
# very first touch of this process.
_SCHEMA_INITIALIZED = False
_SCHEMA_LOCK = threading.Lock()


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    global _SCHEMA_INITIALIZED
    if _SCHEMA_INITIALIZED:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_INITIALIZED:
            return
        # All-or-nothing: if any DDL fails partway, the sentinel stays
        # False and SQLite rolls the rest back so the next call retries.
        with conn:
            conn.executescript(_SCHEMA)
        _SCHEMA_INITIALIZED = True


def _reset_schema_sentinel() -> None:
    """Test helper — re-bootstrap schema on next access."""
    global _SCHEMA_INITIALIZED
    _SCHEMA_INITIALIZED = False


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open a fresh SQLite connection to the receipt DB.

    Path resolution: explicit ``path`` arg first, then the env var via
    ``_shared.db_path()``. The connection is closed at the end of the
    block; SQLite is happy with this pattern and it keeps the in-process
    tools free of long-lived state.
    """
    p = path or db_path()
    _ensure_parent(p)
    conn = sqlite3.connect(p, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_schema(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------- CRUD ----------


def _row_to_entry(row: sqlite3.Row) -> Entry:
    raw_tags = row["tags"] or ""
    tags = tuple(t for t in (s.strip() for s in raw_tags.split(",")) if t)
    return Entry(
        id=row["id"],
        receipt_date=date.fromisoformat(row["receipt_date"]),
        category=row["category"],
        text=row["text"],
        created_at=datetime.fromisoformat(row["created_at"]),
        tags=tags,
    )


def add_entry(
    category: Category,
    text: str,
    receipt_date: date,
    tags: tuple[str, ...] = (),
    *,
    db: Path | None = None,
) -> int:
    if category not in CATEGORIES:
        raise ValueError(f"unknown category {category!r}; expected one of {CATEGORIES}")
    text = text.strip()
    if not text:
        raise ValueError("entry text is empty")
    tag_str = ",".join(t.strip() for t in tags if t.strip())
    now = datetime.now().isoformat(timespec="seconds")
    with connect(db) as conn:
        cur = conn.execute(
            "INSERT INTO entries(receipt_date, category, text, tags, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (receipt_date.isoformat(), category, text, tag_str, now),
        )
        return int(cur.lastrowid)


def delete_entry(entry_id: int, *, db: Path | None = None) -> bool:
    with connect(db) as conn:
        cur = conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        return cur.rowcount > 0


def list_entries(receipt_date: date, *, db: Path | None = None) -> tuple[Entry, ...]:
    with connect(db) as conn:
        rows = conn.execute(
            "SELECT * FROM entries WHERE receipt_date = ? ORDER BY id ASC",
            (receipt_date.isoformat(),),
        ).fetchall()
    return tuple(_row_to_entry(r) for r in rows)


def set_note(receipt_date: date, note: str, *, db: Path | None = None) -> None:
    note = note.strip()
    now = datetime.now().isoformat(timespec="seconds")
    with connect(db) as conn:
        if not note:
            conn.execute("DELETE FROM notes WHERE receipt_date = ?", (receipt_date.isoformat(),))
            return
        conn.execute(
            "INSERT INTO notes(receipt_date, note, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(receipt_date) DO UPDATE SET note=excluded.note, updated_at=excluded.updated_at",
            (receipt_date.isoformat(), note, now),
        )


def get_note(receipt_date: date, *, db: Path | None = None) -> str | None:
    with connect(db) as conn:
        row = conn.execute(
            "SELECT note FROM notes WHERE receipt_date = ?",
            (receipt_date.isoformat(),),
        ).fetchone()
    return row["note"] if row else None


def get_receipt(receipt_date: date, *, db: Path | None = None) -> Receipt:
    entries = list_entries(receipt_date, db=db)
    note = get_note(receipt_date, db=db)
    return Receipt(receipt_date=receipt_date, entries=entries, note=note)


def search(query: str, limit: int = 25, *, db: Path | None = None) -> tuple[Entry, ...]:
    q = query.strip()
    if not q:
        return ()
    with connect(db) as conn:
        rows = conn.execute(
            "SELECT * FROM entries WHERE text LIKE ? OR tags LIKE ? "
            "ORDER BY receipt_date DESC, id DESC LIMIT ?",
            (f"%{q}%", f"%{q}%", limit),
        ).fetchall()
    return tuple(_row_to_entry(r) for r in rows)


def list_dates(limit: int = 30, *, db: Path | None = None) -> tuple[DaySummary, ...]:
    with connect(db) as conn:
        rows = conn.execute(
            "SELECT receipt_date, category, COUNT(*) AS n FROM entries "
            "GROUP BY receipt_date, category "
            "ORDER BY receipt_date DESC, category ASC"
        ).fetchall()
        note_rows = conn.execute("SELECT receipt_date FROM notes").fetchall()
    notes_for = {r["receipt_date"] for r in note_rows}
    by_day: dict[str, dict[Category, int]] = {}
    for r in rows:
        by_day.setdefault(r["receipt_date"], {c: 0 for c in CATEGORIES})
        by_day[r["receipt_date"]][r["category"]] = r["n"]
    ordered = sorted(by_day.keys(), reverse=True)[:limit]
    return tuple(
        DaySummary(
            receipt_date=date.fromisoformat(d),
            counts=by_day[d],
            has_note=d in notes_for,
        )
        for d in ordered
    )
