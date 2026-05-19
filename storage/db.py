"""SQLite layer for hikari-agent.

Schema covers everything memory- and runtime-related:
  - session            : ClaudeSDKClient session_id resume
  - core_blocks        : always-injected persona/state (user_profile, mood_today, ...)
  - facts              : bi-temporal facts about the user (valid_from / valid_to / superseded_by)
  - messages           : raw turn log
  - episodes           : daily-reflection summaries
  - tasks              : open loops as first-class actionable state
  - character_thoughts : Hikari's private diary (never injected; read by reflection only)
  - runtime_state      : misc key/value (silence_until, photos_sent_today, last_user_message, ...)
  - fts                : FTS5 BM25 search over facts + episodes
  - vec_facts          : sqlite-vec KNN index for fact embeddings (384-dim, bge-small)
  - vec_episodes       : sqlite-vec KNN index for episode embeddings
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlite_vec

_DB_PATH = Path(os.environ.get("HIKARI_DB_PATH") or
                Path(__file__).parent.parent / "data" / "hikari.db")

EMBEDDING_DIM = 384

# Shared runtime_state keys — referenced by multiple modules. Import this
# constant rather than typing the literal so renames propagate.
INBOUND_MSG_COUNTER_KEY = "inbound_message_counter"
OUTBOUND_MSG_COUNTER_KEY = "outbound_message_counter"


def _now() -> str:
    return datetime.now(UTC).isoformat()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS session (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    claude_session_id TEXT
);

CREATE TABLE IF NOT EXISTS core_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT UNIQUE NOT NULL,
    content TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    confidence REAL DEFAULT 0.9,
    importance INTEGER DEFAULT 5,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source_message_id INTEGER,
    superseded_by INTEGER REFERENCES facts(id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS facts_active ON facts(subject, predicate) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS facts_subject ON facts(subject);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_ts ON messages(ts);

CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    summary TEXT NOT NULL,
    importance INTEGER DEFAULT 5,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS episodes_date ON episodes(date);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'in_progress', 'completed', 'dropped')),
    due_at TEXT,
    blocked_by INTEGER REFERENCES tasks(id),
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS character_thoughts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thought TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_state (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    content,
    kind UNINDEXED,
    ref_id UNINDEXED
);

CREATE VIRTUAL TABLE IF NOT EXISTS vec_facts USING vec0(
    id INTEGER PRIMARY KEY,
    vec FLOAT[384]
);

CREATE VIRTUAL TABLE IF NOT EXISTS vec_episodes USING vec0(
    id INTEGER PRIMARY KEY,
    vec FLOAT[384]
);

CREATE TABLE IF NOT EXISTS background_tasks (
    task_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    session_id TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    result_summary TEXT,
    cost_usd REAL,
    tool_use_count INTEGER DEFAULT 0,
    meta_json TEXT
);
CREATE INDEX IF NOT EXISTS background_tasks_status ON background_tasks(status);
CREATE INDEX IF NOT EXISTS background_tasks_chat ON background_tasks(chat_id, started_at DESC);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    tool_name TEXT NOT NULL,
    tier INTEGER NOT NULL,
    summary TEXT NOT NULL,
    args_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'timeout')),
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS approvals_pending ON approvals(chat_id, status, created_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    tool TEXT NOT NULL,
    args_json_redacted TEXT NOT NULL,
    result_summary TEXT,
    approved_by TEXT,
    hash_prev TEXT,
    hash_self TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_log_ts ON audit_log(ts DESC);

CREATE TABLE IF NOT EXISTS lexicon (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phrase TEXT UNIQUE NOT NULL,
    source TEXT NOT NULL DEFAULT 'user_coined'
        CHECK (source IN ('user_coined', 'hikari_coined', 'mutual')),
    weight REAL DEFAULT 0.5,
    mention_count INTEGER DEFAULT 1,
    origin_kind TEXT,            -- 'episode' | 'message' | NULL
    origin_id INTEGER,
    last_used_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS lexicon_last_used ON lexicon(last_used_at DESC);
CREATE INDEX IF NOT EXISTS lexicon_phrase ON lexicon(phrase);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,            -- 'pattern_break' | 'recurrence' | 'topic_pattern' | 'absence'
    signature TEXT UNIQUE NOT NULL,  -- stable dedupe key
    summary TEXT NOT NULL,           -- raw text Hikari can reuse
    confidence REAL DEFAULT 0.5,
    last_surfaced_at TEXT,           -- null until first injected
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS observations_last_surfaced ON observations(last_surfaced_at);
CREATE INDEX IF NOT EXISTS observations_kind ON observations(kind);

CREATE TABLE IF NOT EXISTS noticings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal TEXT NOT NULL,            -- e.g. 'sentiment_drop' | 'topic_dropped'
    summary TEXT NOT NULL,           -- one-line in voice
    short_value REAL,
    long_value REAL,
    surfaced_at TEXT,                -- null until injected
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS noticings_unsurfaced ON noticings(surfaced_at, created_at);

CREATE TABLE IF NOT EXISTS peer_representation (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    content_json TEXT NOT NULL,
    version INTEGER DEFAULT 1,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS persona_drift_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER,              -- soft FK to messages.id
    text_snippet TEXT NOT NULL,      -- first 300 chars of outbound reply
    score REAL NOT NULL,             -- 0-1, 1=pure Hikari, 0=full assistant drift
    class_label TEXT NOT NULL,       -- 'hikari' | 'drifting' | 'unclear'
    rubric_version INTEGER DEFAULT 1,
    payload TEXT,                    -- raw judge output for audit
    sampled_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS drift_sampled_at ON persona_drift_scores(sampled_at DESC);
"""


# Process-level sentinel: schema setup + idempotent migrations only run on the
# first _conn() call per process. SQLite WAL covers cross-process safety; this
# sentinel eliminates per-connection PRAGMA table_info + bookkeeping reads from
# the steady-state path. Reset via ``_reset_schema_sentinel()`` in test fixtures.
_SCHEMA_INITIALIZED = False


def _reset_schema_sentinel() -> None:
    """Test helper — clears the process-level migration cache so test fixtures
    that swap ``_DB_PATH`` rerun migrations against the fresh per-test DB."""
    global _SCHEMA_INITIALIZED
    _SCHEMA_INITIALIZED = False


def _ensure_schema(conn: sqlite3.Connection) -> None:
    global _SCHEMA_INITIALIZED
    if _SCHEMA_INITIALIZED:
        return
    for stmt in _SCHEMA.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)
    _migrate_tasks_decay_columns(conn)
    _SCHEMA_INITIALIZED = True


def _migrate_tasks_decay_columns(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add open-loop decay columns to ``tasks`` if missing.
    SQLite has no ``IF NOT EXISTS`` on ALTER COLUMN, so we sniff via PRAGMA."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if "importance" not in existing:
        conn.execute("ALTER TABLE tasks ADD COLUMN importance INTEGER DEFAULT 5")
    if "mention_count" not in existing:
        conn.execute("ALTER TABLE tasks ADD COLUMN mention_count INTEGER DEFAULT 0")
    if "last_mention_at" not in existing:
        conn.execute("ALTER TABLE tasks ADD COLUMN last_mention_at TEXT")
    _migrate_approvals_defer_columns(conn)
    _migrate_user_profile_to_peer_representation(conn)


def _migrate_user_profile_to_peer_representation(conn: sqlite3.Connection) -> None:
    """Phase 7 idempotent migration: if the legacy ``core_blocks.user_profile``
    row exists and the new ``peer_representation`` table is empty, copy the
    content over as the ``summary`` field. Leaves the old row in place (the
    hook formatter filters it out at read time) so any external readers don't
    break — daily reflection will gradually shift writes to the new table.
    """
    import json
    # Bail if peer_representation already has a row (don't clobber).
    existing = conn.execute(
        "SELECT 1 FROM peer_representation WHERE id = 1"
    ).fetchone()
    if existing:
        return
    legacy = conn.execute(
        "SELECT content FROM core_blocks WHERE label = 'user_profile'"
    ).fetchone()
    if not legacy or not legacy["content"]:
        return
    seed = {
        "communication_style": "",
        "values": [],
        "domain_expertise": [],
        "current_concerns": [],
        "blindspots": [],
        "summary": str(legacy["content"]).strip()[:1000],
    }
    conn.execute(
        "INSERT INTO peer_representation (id, content_json, version, updated_at) "
        "VALUES (1, ?, 1, ?)",
        (json.dumps(seed, ensure_ascii=False), _now()),
    )


def _migrate_approvals_defer_columns(conn: sqlite3.Connection) -> None:
    """Phase 6: add SDK-defer fields to ``approvals`` so we can persist the
    deferred tool call (tool_use_id + tool_input) for resume-after-y."""
    existing = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(approvals)").fetchall()
    }
    if "deferred_tool_use_id" not in existing:
        conn.execute("ALTER TABLE approvals ADD COLUMN deferred_tool_use_id TEXT")
    if "deferred_tool_name" not in existing:
        conn.execute("ALTER TABLE approvals ADD COLUMN deferred_tool_name TEXT")
    if "deferred_tool_input_json" not in existing:
        conn.execute(
            "ALTER TABLE approvals ADD COLUMN deferred_tool_input_json TEXT"
        )


@contextmanager
def _conn():
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(_DB_PATH)
    c.row_factory = sqlite3.Row
    c.enable_load_extension(True)
    sqlite_vec.load(c)
    c.enable_load_extension(False)
    # WAL gives us readers-don't-block-writers semantics, which matters for the
    # daily decay sweep + the dispatch worker writing in parallel.
    try:
        c.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        # In-memory DBs reject WAL; fall back silently.
        pass
    try:
        _ensure_schema(c)
        yield c
        c.commit()
    finally:
        c.close()


# ---------- session ----------

def get_session_id() -> str | None:
    with _conn() as c:
        row = c.execute("SELECT claude_session_id FROM session WHERE id = 1").fetchone()
    return row["claude_session_id"] if row and row["claude_session_id"] else None


def set_session_id(session_id: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO session (id, claude_session_id) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET claude_session_id = excluded.claude_session_id",
            (session_id,),
        )


# ---------- core_blocks ----------

def upsert_core_block(label: str, content: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO core_blocks (label, content, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(label) DO UPDATE SET content = excluded.content, "
            "updated_at = excluded.updated_at",
            (label, content, _now()),
        )


def get_core_block(label: str) -> str | None:
    with _conn() as c:
        row = c.execute("SELECT content FROM core_blocks WHERE label = ?", (label,)).fetchone()
    return row["content"] if row else None


def all_core_blocks() -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT label, content, updated_at FROM core_blocks ORDER BY label"
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- facts ----------

def insert_fact(
    subject: str,
    predicate: str,
    object_: str,
    importance: int = 5,
    confidence: float = 0.9,
    source_message_id: int | None = None,
) -> int:
    """Insert a new fact. Returns row id. Caller is responsible for any
    contradiction/supersession logic — this function does NOT auto-supersede."""
    now = _now()
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO facts (subject, predicate, object, confidence, importance, "
            "valid_from, source_message_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (subject, predicate, object_, confidence, importance, now, source_message_id, now),
        )
        fact_id = cur.lastrowid
        c.execute(
            "INSERT INTO fts (content, kind, ref_id) VALUES (?, 'fact', ?)",
            (f"{subject} {predicate} {object_}", fact_id),
        )
    return fact_id


def active_facts_matching(subject: str, predicate: str) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM facts WHERE subject = ? AND predicate = ? AND valid_to IS NULL",
            (subject, predicate),
        ).fetchall()
    return [dict(r) for r in rows]


def supersede_fact(old_id: int, new_id: int, reason: str | None = None) -> None:
    """Mark old fact invalid (valid_to=now, superseded_by=new_id)."""
    with _conn() as c:
        c.execute(
            "UPDATE facts SET valid_to = ?, superseded_by = ? WHERE id = ?",
            (_now(), new_id, old_id),
        )
        c.execute("DELETE FROM fts WHERE kind = 'fact' AND ref_id = ?", (old_id,))
        c.execute("DELETE FROM vec_facts WHERE id = ?", (old_id,))
        if reason:
            c.execute(
                "INSERT INTO character_thoughts (thought, created_at) VALUES (?, ?)",
                (f"superseded fact #{old_id} -> #{new_id}: {reason}", _now()),
            )


def invalidate_fact(fact_id: int, reason: str | None = None) -> None:
    """Mark a fact invalid without a superseding row (e.g. wrong fact entirely)."""
    with _conn() as c:
        c.execute("UPDATE facts SET valid_to = ? WHERE id = ?", (_now(), fact_id))
        c.execute("DELETE FROM fts WHERE kind = 'fact' AND ref_id = ?", (fact_id,))
        c.execute("DELETE FROM vec_facts WHERE id = ?", (fact_id,))
        if reason:
            c.execute(
                "INSERT INTO character_thoughts (thought, created_at) VALUES (?, ?)",
                (f"invalidated fact #{fact_id}: {reason}", _now()),
            )


def active_facts(limit: int = 100) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM facts WHERE valid_to IS NULL ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_fact(fact_id: int) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
    return dict(row) if row else None


# ---------- messages ----------

def append_message(role: str, content: str) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO messages (role, content, ts) VALUES (?, ?, ?)",
            (role, content, _now()),
        )
    return cur.lastrowid


def recent_messages(limit: int = 20) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM messages ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


# ---------- episodes ----------

def insert_episode(date: str, summary: str, importance: int = 5) -> int:
    now = _now()
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO episodes (date, summary, importance, created_at) "
            "VALUES (?, ?, ?, ?)",
            (date, summary, importance, now),
        )
        episode_id = cur.lastrowid
        c.execute(
            "INSERT INTO fts (content, kind, ref_id) VALUES (?, 'episode', ?)",
            (summary, episode_id),
        )
    return episode_id


def recent_episodes(limit: int = 3) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM episodes ORDER BY date DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_episode(episode_id: int) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    return dict(row) if row else None


def prune_episodes_older_than_days(days: int) -> int:
    """Delete episodes whose date is older than `days` from today. Returns count."""
    from datetime import date as _date
    from datetime import timedelta

    cutoff = (_date.today() - timedelta(days=days)).isoformat()
    with _conn() as c:
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM episodes WHERE date < ?", (cutoff,)
        ).fetchall()]
        if ids:
            qs = ",".join("?" * len(ids))
            c.execute(f"DELETE FROM episodes WHERE id IN ({qs})", ids)
            c.execute(f"DELETE FROM fts WHERE kind = 'episode' AND ref_id IN ({qs})", ids)
            c.execute(f"DELETE FROM vec_episodes WHERE id IN ({qs})", ids)
    return len(ids)


# ---------- tasks ----------

def create_task(subject: str, description: str | None = None,
                due_at: str | None = None, importance: int = 5) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO tasks (subject, description, due_at, importance, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (subject, description, due_at, max(1, min(10, int(importance))), _now()),
        )
    return cur.lastrowid


def task_record_mention(task_id: int) -> int:
    """Increment mention_count + bump last_mention_at. Returns new count."""
    with _conn() as c:
        c.execute(
            "UPDATE tasks SET mention_count = COALESCE(mention_count, 0) + 1, "
            "last_mention_at = ? WHERE id = ?",
            (_now(), task_id),
        )
        row = c.execute(
            "SELECT mention_count FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    return int(row["mention_count"] or 0) if row else 0


def task_decay_sweep(
    half_life_by_importance: dict[int, int],
    default_half_life_days: int,
    max_mentions_before_drop: int,
) -> tuple[int, int]:
    """Drop pending tasks past their decay horizon or mention cap.

    Returns ``(decayed_dropped, mention_dropped)``. A task is decayed if
    ``now - created_at > 2 × half_life_days_for_importance``.
    """

    decayed = 0
    mention_dropped = 0
    now = datetime.now(UTC)
    with _conn() as c:
        rows = c.execute(
            "SELECT id, importance, created_at, mention_count FROM tasks "
            "WHERE status IN ('pending', 'in_progress')"
        ).fetchall()
        for row in rows:
            importance = int(row["importance"] or 5)
            half_life = int(half_life_by_importance.get(importance, default_half_life_days))
            cutoff_age_days = 2 * half_life
            try:
                created = datetime.fromisoformat(row["created_at"])
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
            except (ValueError, TypeError):
                continue
            age_days = (now - created).days
            if age_days > cutoff_age_days:
                c.execute(
                    "UPDATE tasks SET status = 'dropped', resolved_at = ? WHERE id = ?",
                    (_now(), int(row["id"])),
                )
                decayed += 1
                continue
            mentions = int(row["mention_count"] or 0)
            if mentions >= max_mentions_before_drop:
                c.execute(
                    "UPDATE tasks SET status = 'dropped', resolved_at = ? WHERE id = ?",
                    (_now(), int(row["id"])),
                )
                mention_dropped += 1
    # Decayed tasks get a thought entry so reflection can notice the churn.
    if decayed or mention_dropped:
        append_thought(
            f"task decay sweep: dropped {decayed} aged + "
            f"{mention_dropped} over-mentioned. moving on."
        )
    return decayed, mention_dropped


def update_task(task_id: int, status: str | None = None,
                blocked_by: int | None = None) -> None:
    sets: list[str] = []
    args: list[Any] = []
    if status:
        sets.append("status = ?")
        args.append(status)
        if status in ("completed", "dropped"):
            sets.append("resolved_at = ?")
            args.append(_now())
    if blocked_by is not None:
        sets.append("blocked_by = ?")
        args.append(blocked_by)
    if not sets:
        return
    args.append(task_id)
    with _conn() as c:
        c.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", args)


def open_tasks() -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM tasks WHERE status IN ('pending', 'in_progress') "
            "ORDER BY due_at NULLS LAST, created_at"
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- character_thoughts (private diary) ----------

def append_thought(thought: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO character_thoughts (thought, created_at) VALUES (?, ?)",
            (thought, _now()),
        )


def prune_thoughts_older_than_days(days: int) -> int:
    """Delete character_thoughts older than `days` from now. Returns count."""
    from datetime import timedelta

    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM character_thoughts WHERE created_at < ?",
            (cutoff,),
        )
        return cur.rowcount or 0


# ---------- lexicon (shared private vocabulary) ----------

def lexicon_record(
    phrase: str,
    source: str = "user_coined",
    origin_kind: str | None = None,
    origin_id: int | None = None,
    weight: float = 0.5,
) -> int:
    """Insert or bump a lexicon entry. Returns row id.

    On conflict (phrase already present), bumps mention_count + last_used_at and
    nudges weight upward (saturating at 1.0).
    """
    now = _now()
    with _conn() as c:
        existing = c.execute(
            "SELECT id, weight, mention_count FROM lexicon WHERE phrase = ?",
            (phrase,),
        ).fetchone()
        if existing:
            new_weight = min(1.0, float(existing["weight"] or 0.5) + 0.1)
            c.execute(
                "UPDATE lexicon SET mention_count = COALESCE(mention_count, 0) + 1, "
                "last_used_at = ?, weight = ? WHERE id = ?",
                (now, new_weight, int(existing["id"])),
            )
            return int(existing["id"])
        cur = c.execute(
            "INSERT INTO lexicon "
            "(phrase, source, weight, mention_count, origin_kind, origin_id, "
            " last_used_at, created_at) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
            (phrase, source, max(0.0, min(1.0, weight)),
             origin_kind, origin_id, now, now),
        )
        return cur.lastrowid


def lexicon_top(limit: int = 5, half_life_days: float = 14.0) -> list[dict[str, Any]]:
    """Return top lexicon entries scored by ``weight × exp(-age_days/half_life)``.

    Order by score desc. Excludes entries with weight <= 0.
    """
    import math
    now = datetime.now(UTC)
    with _conn() as c:
        rows = c.execute(
            "SELECT id, phrase, source, weight, mention_count, last_used_at "
            "FROM lexicon WHERE weight > 0"
        ).fetchall()
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        try:
            ts = datetime.fromisoformat(row["last_used_at"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            age_days = max(0.0, (now - ts).total_seconds() / 86400)
        except (ValueError, TypeError):
            age_days = 1e6
        score = float(row["weight"] or 0.0) * math.exp(-age_days / max(0.1, half_life_days))
        scored.append((score, {**dict(row), "score": score}))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:limit]]


def lexicon_decay_and_prune(
    decay_per_call: float = 0.02,
    min_weight: float = 0.05,
) -> tuple[int, int]:
    """Apply a small downward step to every stored weight, then delete entries
    that fell below the floor. Run from daily reflection so the weight column
    remains a useful relative signal instead of saturating at 1.0 forever.

    Returns ``(decayed_rows, pruned_rows)``.
    """
    with _conn() as c:
        cur = c.execute(
            "UPDATE lexicon SET weight = MAX(0.0, weight - ?)",
            (float(decay_per_call),),
        )
        decayed = cur.rowcount or 0
        cur2 = c.execute(
            "DELETE FROM lexicon WHERE weight < ?",
            (float(min_weight),),
        )
        pruned = cur2.rowcount or 0
    return decayed, pruned


def lexicon_prune_stale(min_weight: float = 0.05) -> int:
    """Hard prune entries whose stored weight is already below the floor.
    Most callers want :func:`lexicon_decay_and_prune` instead — this is for
    cases where the weight has been explicitly demoted by other logic."""
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM lexicon WHERE weight < ?",
            (float(min_weight),),
        )
        return cur.rowcount or 0


def lexicon_get(phrase: str) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM lexicon WHERE phrase = ?", (phrase,)
        ).fetchone()
    return dict(row) if row else None


def all_messages_text_since(iso_cutoff: str, role: str | None = None) -> list[str]:
    """Helper for lexicon extraction: return raw message texts since cutoff."""
    sql = "SELECT content FROM messages WHERE ts >= ?"
    args: list[Any] = [iso_cutoff]
    if role:
        sql += " AND role = ?"
        args.append(role)
    sql += " ORDER BY ts"
    with _conn() as c:
        rows = c.execute(sql, args).fetchall()
    return [str(r["content"] or "") for r in rows]


# ---------- observations (Hikari-noticeable patterns) ----------

def observation_record(
    kind: str,
    signature: str,
    summary: str,
    confidence: float = 0.6,
) -> int:
    """Upsert by signature — dedupes pattern restatements across reflections.

    On conflict, only the summary + confidence are refreshed; ``last_surfaced_at``
    is preserved so we don't re-surface the same pattern back-to-back.
    """
    now = _now()
    with _conn() as c:
        existing = c.execute(
            "SELECT id FROM observations WHERE signature = ?", (signature,)
        ).fetchone()
        if existing:
            c.execute(
                "UPDATE observations SET summary = ?, confidence = ? WHERE id = ?",
                (summary, max(0.0, min(1.0, float(confidence))), int(existing["id"])),
            )
            return int(existing["id"])
        cur = c.execute(
            "INSERT INTO observations (kind, signature, summary, confidence, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (kind, signature, summary,
             max(0.0, min(1.0, float(confidence))), now),
        )
        return cur.lastrowid


def observations_unsurfaced(
    min_confidence: float = 0.6,
    limit: int = 1,
    re_surface_min_days: int = 7,
) -> list[dict[str, Any]]:
    """Return observations either never surfaced or surfaced long enough ago.

    Older surfaced entries become re-eligible after ``re_surface_min_days``.
    """
    from datetime import timedelta
    cutoff = (datetime.now(UTC) - timedelta(days=re_surface_min_days)).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM observations "
            "WHERE confidence >= ? "
            "AND (last_surfaced_at IS NULL OR last_surfaced_at < ?) "
            "ORDER BY confidence DESC, created_at DESC LIMIT ?",
            (float(min_confidence), cutoff, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def observation_mark_surfaced(observation_id: int) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE observations SET last_surfaced_at = ? WHERE id = ?",
            (_now(), observation_id),
        )


# ---------- noticings (week-over-week user-state deltas) ----------

def noticing_record(
    signal: str,
    summary: str,
    short_value: float | None = None,
    long_value: float | None = None,
) -> int:
    """Insert a noticing. Caller is responsible for deduping at write time."""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO noticings (signal, summary, short_value, long_value, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (signal, summary, short_value, long_value, _now()),
        )
    return cur.lastrowid


def noticings_unsurfaced(limit: int = 1) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM noticings WHERE surfaced_at IS NULL "
            "ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def noticing_mark_surfaced(noticing_id: int) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE noticings SET surfaced_at = ? WHERE id = ?",
            (_now(), noticing_id),
        )


def prune_noticings_older_than_days(days: int) -> int:
    from datetime import timedelta
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM noticings WHERE created_at < ?", (cutoff,)
        )
    return cur.rowcount or 0


# ---------- persona_drift_scores (Haiku-judge telemetry) ----------

def drift_record(
    text_snippet: str,
    score: float,
    class_label: str,
    message_id: int | None = None,
    rubric_version: int = 1,
    payload: str | None = None,
) -> int:
    """Append a drift sample. Returns row id."""
    snippet = (text_snippet or "")[:300]
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO persona_drift_scores "
            "(message_id, text_snippet, score, class_label, rubric_version, "
            " payload, sampled_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (message_id, snippet, max(0.0, min(1.0, float(score))),
             class_label, int(rubric_version), payload, _now()),
        )
    return cur.lastrowid


def drift_recent_avg(window_days: int = 7) -> float | None:
    """Mean of `score` across the last `window_days`. Returns None if no samples."""
    from datetime import timedelta
    cutoff = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
    with _conn() as c:
        row = c.execute(
            "SELECT AVG(score) AS avg, COUNT(*) AS n FROM persona_drift_scores "
            "WHERE sampled_at >= ?",
            (cutoff,),
        ).fetchone()
    if not row or not row["n"]:
        return None
    return float(row["avg"])


def drift_recent_below_threshold(
    threshold: float = 0.5,
    window_days: int = 7,
) -> int:
    """Count of samples whose score is below `threshold` in the window."""
    from datetime import timedelta
    cutoff = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM persona_drift_scores "
            "WHERE sampled_at >= ? AND score < ?",
            (cutoff, float(threshold)),
        ).fetchone()
    return int(row["n"] or 0)


def drift_count_today() -> int:
    """Count of samples taken today (UTC). Used to enforce daily cap."""
    today_iso = datetime.now(UTC).date().isoformat()
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM persona_drift_scores "
            "WHERE substr(sampled_at, 1, 10) = ?",
            (today_iso,),
        ).fetchone()
    return int(row["n"] or 0)


def get_peer_representation() -> dict[str, Any] | None:
    """Return the structured user model as a dict, or None if not yet populated.

    The peer_representation table is a single-row table (id always = 1).
    Replaces the flat ``core_blocks.user_profile`` dump with a Honcho-style
    structured shape (communication_style / values / domain_expertise /
    current_concerns / blindspots / summary). ``mood_today`` stays on the
    ``core_blocks`` fast path — three readers depend on its low latency.
    """
    import json
    with _conn() as c:
        row = c.execute(
            "SELECT content_json FROM peer_representation WHERE id = 1"
        ).fetchone()
    if not row or not row["content_json"]:
        return None
    try:
        data = json.loads(row["content_json"])
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def upsert_peer_representation(content: dict[str, Any]) -> None:
    """Persist the structured user model. Overwrites on conflict — caller is
    responsible for merge-before-upsert via ``peer_model.merge_dialectic``."""
    import json
    if not isinstance(content, dict):
        raise TypeError(f"peer_representation content must be dict, got {type(content)}")
    with _conn() as c:
        c.execute(
            "INSERT INTO peer_representation (id, content_json, version, updated_at) "
            "VALUES (1, ?, 1, ?) "
            "ON CONFLICT(id) DO UPDATE SET content_json = excluded.content_json, "
            "updated_at = excluded.updated_at",
            (json.dumps(content, ensure_ascii=False), _now()),
        )


def prune_drift_older_than_days(days: int) -> int:
    from datetime import timedelta
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM persona_drift_scores WHERE sampled_at < ?", (cutoff,)
        )
    return cur.rowcount or 0


# ---------- runtime_state (misc kv) ----------

def runtime_get(key: str) -> str | None:
    with _conn() as c:
        row = c.execute("SELECT value FROM runtime_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def runtime_set(key: str, value: str | int | float | None) -> None:
    with _conn() as c:
        if value is None:
            c.execute("DELETE FROM runtime_state WHERE key = ?", (key,))
        else:
            c.execute(
                "INSERT INTO runtime_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )


def runtime_get_int(key: str, default: int = 0) -> int:
    raw = runtime_get(key)
    try:
        return int(raw) if raw is not None else default
    except (ValueError, TypeError):
        return default


# ---------- background_tasks (long-running dispatched work) ----------

def bg_task_create(
    task_id: str,
    kind: str,
    chat_id: int,
    prompt: str,
    meta: dict[str, Any] | None = None,
) -> None:
    import json
    with _conn() as c:
        c.execute(
            "INSERT INTO background_tasks "
            "(task_id, kind, chat_id, prompt, status, started_at, meta_json) "
            "VALUES (?, ?, ?, ?, 'queued', ?, ?)",
            (task_id, kind, chat_id, prompt, _now(),
             json.dumps(meta) if meta else None),
        )


def bg_task_update(task_id: str, **fields: Any) -> None:
    if not fields:
        return
    allowed = {"status", "session_id", "completed_at", "result_summary",
               "cost_usd", "tool_use_count"}
    sets = []
    args = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append(f"{k} = ?")
        args.append(v)
    if not sets:
        return
    args.append(task_id)
    with _conn() as c:
        c.execute(
            f"UPDATE background_tasks SET {', '.join(sets)} WHERE task_id = ?",
            args,
        )


def bg_task_get(task_id: str) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM background_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
    return dict(row) if row else None


def bg_tasks_running() -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM background_tasks WHERE status IN ('queued', 'running') "
            "ORDER BY started_at"
        ).fetchall()
    return [dict(r) for r in rows]


def bg_tasks_recent(chat_id: int, limit: int = 10) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM background_tasks WHERE chat_id = ? "
            "ORDER BY started_at DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- approvals (gated tool calls) ----------

def approval_create(chat_id: int, tool_name: str, tier: int,
                    summary: str, args: dict[str, Any]) -> int:
    import json
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO approvals "
            "(chat_id, tool_name, tier, summary, args_json, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (chat_id, tool_name, tier, summary, json.dumps(args), _now()),
        )
    return cur.lastrowid


def approval_create_deferred(
    chat_id: int,
    tool_name: str,
    tier: int,
    summary: str,
    args: dict[str, Any],
    deferred_tool_use_id: str,
    deferred_tool_input: dict[str, Any],
) -> int:
    """Phase 6: write an approval row tagged with SDK-defer fields.

    The row carries the original tool_use_id + tool_input so the resume path
    can reconstruct the call after the user replies. Distinguishable from a
    legacy approval row by ``deferred_tool_use_id IS NOT NULL``.
    """
    import json
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO approvals "
            "(chat_id, tool_name, tier, summary, args_json, status, created_at, "
            " deferred_tool_use_id, deferred_tool_name, deferred_tool_input_json) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
            (chat_id, tool_name, tier, summary, json.dumps(args), _now(),
             deferred_tool_use_id, tool_name,
             json.dumps(deferred_tool_input)),
        )
    return cur.lastrowid


def approvals_pending_deferred() -> list[dict[str, Any]]:
    """Return all pending approvals that carry a deferred_tool_use_id.

    Used at bot startup to resurface prompts that were live when the bot died.
    """
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM approvals "
            "WHERE status = 'pending' AND deferred_tool_use_id IS NOT NULL "
            "ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def approval_pending_for(chat_id: int) -> dict[str, Any] | None:
    """Return the oldest still-pending approval for this chat, or None."""
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM approvals WHERE chat_id = ? AND status = 'pending' "
            "ORDER BY created_at LIMIT 1",
            (chat_id,),
        ).fetchone()
    return dict(row) if row else None


def approval_resolve(approval_id: int, status: str) -> None:
    """status: 'approved' | 'rejected' | 'timeout'."""
    with _conn() as c:
        c.execute(
            "UPDATE approvals SET status = ?, resolved_at = ? WHERE id = ?",
            (status, _now(), approval_id),
        )


# ---------- audit_log (hash-chained tool-call ledger) ----------

def audit_append(tool: str, args_json_redacted: str,
                 result_summary: str | None = None,
                 approved_by: str | None = None) -> int:
    """Append a hash-chained audit row. Returns id."""
    import hashlib
    import json
    ts = _now()
    with _conn() as c:
        prev_row = c.execute(
            "SELECT hash_self FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        hash_prev = prev_row["hash_self"] if prev_row else ""
        material = json.dumps([ts, tool, args_json_redacted,
                               result_summary or "", approved_by or "",
                               hash_prev], sort_keys=True)
        hash_self = hashlib.sha256(material.encode()).hexdigest()
        cur = c.execute(
            "INSERT INTO audit_log "
            "(ts, tool, args_json_redacted, result_summary, approved_by, hash_prev, hash_self) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, tool, args_json_redacted, result_summary,
             approved_by, hash_prev, hash_self),
        )
    return cur.lastrowid


# ---------- FTS5 BM25 search ----------

def fts_search(query: str, limit: int = 30) -> list[dict[str, Any]]:
    """Return BM25-ranked hits with kind + ref_id + bm25 score (lower = better)."""
    with _conn() as c:
        try:
            rows = c.execute(
                "SELECT kind, ref_id, bm25(fts) AS rank, content "
                "FROM fts WHERE fts MATCH ? ORDER BY rank LIMIT ?",
                (query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    return [dict(r) for r in rows]


# ---------- vector search (sqlite-vec) ----------

def set_vec_fact(fact_id: int, embedding: list[float]) -> None:
    if not embedding or len(embedding) != EMBEDDING_DIM:
        return
    with _conn() as c:
        c.execute("DELETE FROM vec_facts WHERE id = ?", (fact_id,))
        c.execute(
            "INSERT INTO vec_facts(id, vec) VALUES (?, ?)",
            (fact_id, sqlite_vec.serialize_float32(embedding)),
        )


def set_vec_episode(episode_id: int, embedding: list[float]) -> None:
    if not embedding or len(embedding) != EMBEDDING_DIM:
        return
    with _conn() as c:
        c.execute("DELETE FROM vec_episodes WHERE id = ?", (episode_id,))
        c.execute(
            "INSERT INTO vec_episodes(id, vec) VALUES (?, ?)",
            (episode_id, sqlite_vec.serialize_float32(embedding)),
        )


def vec_search(table: str, query_vec: list[float], k: int = 30) -> list[dict[str, Any]]:
    """KNN search against a vec0 virtual table. Returns rows with id + distance (L2,
    lower = closer)."""
    if table not in ("vec_facts", "vec_episodes"):
        raise ValueError(f"unsupported vec table: {table}")
    if not query_vec or len(query_vec) != EMBEDDING_DIM:
        return []
    with _conn() as c:
        rows = c.execute(
            f"SELECT id, distance FROM {table} "
            f"WHERE vec MATCH ? AND k = ? ORDER BY distance",
            (sqlite_vec.serialize_float32(query_vec), k),
        ).fetchall()
    return [dict(r) for r in rows]


def ids_without_embedding(table: str) -> list[int]:
    """Used by backfill — find rows in facts/episodes that lack a vec entry."""
    if table == "facts":
        sql = (
            "SELECT id FROM facts WHERE valid_to IS NULL "
            "AND id NOT IN (SELECT id FROM vec_facts)"
        )
    elif table == "episodes":
        sql = (
            "SELECT id FROM episodes "
            "WHERE id NOT IN (SELECT id FROM vec_episodes)"
        )
    else:
        raise ValueError(f"unsupported table: {table}")
    with _conn() as c:
        rows = c.execute(sql).fetchall()
    return [r["id"] for r in rows]


# ---------- bulk helpers (for migration) ----------

def bulk_insert_facts(rows: Iterable[dict[str, Any]]) -> int:
    n = 0
    with _conn() as c:
        for r in rows:
            cur = c.execute(
                "INSERT INTO facts (subject, predicate, object, confidence, importance, "
                "valid_from, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    r["subject"], r["predicate"], r["object"],
                    r.get("confidence", 0.7),
                    r.get("importance", 5),
                    r.get("valid_from", _now()),
                    r.get("created_at", _now()),
                ),
            )
            c.execute(
                "INSERT INTO fts (content, kind, ref_id) VALUES (?, 'fact', ?)",
                (f"{r['subject']} {r['predicate']} {r['object']}", cur.lastrowid),
            )
            n += 1
    return n


def bulk_insert_episodes(rows: Iterable[dict[str, Any]]) -> int:
    n = 0
    with _conn() as c:
        for r in rows:
            cur = c.execute(
                "INSERT INTO episodes (date, summary, importance, created_at) "
                "VALUES (?, ?, ?, ?)",
                (r["date"], r["summary"], r.get("importance", 5),
                 r.get("created_at", _now())),
            )
            c.execute(
                "INSERT INTO fts (content, kind, ref_id) VALUES (?, 'episode', ?)",
                (r["summary"], cur.lastrowid),
            )
            n += 1
    return n
