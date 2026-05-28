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

import hashlib
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlite_vec

from storage.migrations import backfill_if_needed, run_once

logger = logging.getLogger(__name__)

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
    superseded_by_fact_id INTEGER REFERENCES facts(id),
    status TEXT NOT NULL DEFAULT 'active',
    source TEXT,
    last_recalled_at TEXT,
    recall_hit_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS facts_active ON facts(subject, predicate) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS facts_subject ON facts(subject);
-- facts_status index lives in _migrate_facts_bitemporal — created after the
-- `status` column is ALTER-added, so prod DBs predating bi-temporal migration
-- don't blow up on the schema bootstrap pass.

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

-- Phase 8: 👍/👎 reactions from the user on Hikari's outbound messages.
-- Keyed by the Telegram outbound message_id (stored on messages.telegram_message_id
-- by the bridge after a successful reply_text). Used by reflection to compare
-- the drift judge's scores against user feedback — when they diverge, the rubric
-- needs tuning, not the bot.
CREATE TABLE IF NOT EXISTS user_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_message_id INTEGER NOT NULL,
    rating INTEGER NOT NULL CHECK (rating IN (-1, 1)),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS user_feedback_msg ON user_feedback(telegram_message_id);
CREATE INDEX IF NOT EXISTS user_feedback_created ON user_feedback(created_at DESC);

-- Phase 10: scheduled reminders. Fired by the reminders_fire scheduler job
-- (storage.db.reminder_due returns rows whose effective fire time has passed).
-- Optional Google Calendar mirror via gcal_event_id, drained by the
-- reminders_gcal_sync job (separate from the fire job so reminder_create
-- returns immediately without an LLM round-trip).
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fire_at TEXT NOT NULL,
    lead_minutes INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL,
    repeat TEXT,
    gcal_event_id TEXT,
    gcal_sync_pending INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    fired_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, fire_at);

-- Accountability items: link a primary reminder to a follow-up check.
-- outcome NULL = pending, 0 = didn't do it, 1 = did it.
CREATE TABLE IF NOT EXISTS accountability_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reminder_id INTEGER NOT NULL REFERENCES reminders(id),
    follow_up_reminder_id INTEGER NOT NULL REFERENCES reminders(id),
    task_text TEXT NOT NULL,
    outcome INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_accountability_followup
    ON accountability_items(follow_up_reminder_id);
CREATE INDEX IF NOT EXISTS idx_accountability_unresolved
    ON accountability_items(outcome) WHERE outcome IS NULL;

-- Legacy table kept only so the validity-columns migration can ALTER it on
-- fresh DBs. Dropped by _migrate_drop_episode_summaries_and_fact_relations.
CREATE TABLE IF NOT EXISTS fact_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_fact_id INTEGER NOT NULL,
    predicate TEXT NOT NULL,
    object_fact_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (subject_fact_id) REFERENCES facts(id),
    FOREIGN KEY (object_fact_id) REFERENCES facts(id)
);

-- Phase 11: weekly sleep-time consolidation archive (Letta sleep-time pattern,
-- Apr 2025). The current week's consolidation lives in core_blocks under the
-- ``weekly_consolidation`` label so it flows into the system prompt every turn;
-- when a new weekly pass runs, the previous core_block content is snapshotted
-- here before being overwritten. Lets us reconstruct the trail of week-over-
-- week deltas without bloating the always-on prompt.
CREATE TABLE IF NOT EXISTS weekly_consolidations_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_ending TEXT NOT NULL,
    summary_text TEXT NOT NULL,
    episode_count INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_weekly_consolidations_week
    ON weekly_consolidations_archive(week_ending);

-- Phase 11: per-session scratch memory shared by subagents (recall + wiki etc.).
-- Hindsight pattern (May 2026). 24h TTL enforced by scratch_cleanup_old (daily
-- reflection). 100-row cap per session enforced by scratch_put.
-- Session-scoped: entries from one session never bleed into another.
CREATE TABLE IF NOT EXISTS session_scratch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_session_scratch_session_topic
    ON session_scratch(session_id, topic);

-- T7.2: per-photo geolocation history (from EXIF GPS reverse-geocoded via
-- Nominatim). Populated by the bridge when a user uploads a photo as a
-- document (Telegram strips EXIF from compressed photos but preserves it
-- on document uploads). Used by proactive.detect_recurring_location_pattern
-- to spot repeat visits.
CREATE TABLE IF NOT EXISTS photo_locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    label TEXT,
    taken_at TEXT,
    received_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_photo_locations_received ON photo_locations(received_at);

-- Phase 14: OAuth 2.1 + PKCE + DCR for the external MCP server. Tables are
-- brand-new (no ALTER ADD COLUMN), so indexes live in _SCHEMA directly — the
-- "indexes in migration fn" rule only applies to ALTER-added columns.
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id TEXT PRIMARY KEY,
    client_name TEXT,
    redirect_uris TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS oauth_codes (
    code TEXT PRIMARY KEY,
    client_id TEXT NOT NULL REFERENCES oauth_clients(client_id),
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    code_challenge_method TEXT NOT NULL,
    scope TEXT,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS oauth_codes_expires_at ON oauth_codes(expires_at);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    token TEXT PRIMARY KEY,
    client_id TEXT NOT NULL REFERENCES oauth_clients(client_id),
    token_type TEXT NOT NULL CHECK (token_type IN ('access', 'refresh')),
    parent_token TEXT,
    scope TEXT,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    last_used_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS oauth_tokens_active
    ON oauth_tokens(token) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS oauth_tokens_expires_at ON oauth_tokens(expires_at);
CREATE INDEX IF NOT EXISTS oauth_tokens_parent ON oauth_tokens(parent_token)
    WHERE parent_token IS NOT NULL;

CREATE TABLE IF NOT EXISTS oauth_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    event_type TEXT NOT NULL,
    client_id TEXT,
    ip TEXT,
    details_json TEXT
);
CREATE INDEX IF NOT EXISTS oauth_audit_log_ts ON oauth_audit_log(ts DESC);

-- Drift canary: weekly out-of-band probe asking Hikari one of three rotating
-- questions targeting her hard opinions (needs_no_one / liking_embarrassing /
-- attention_mech). LLM-as-judge classifies the answer as hold/partial/drift
-- and on 'drift' the scheduler sends an operator-style heartbeat alert.
-- Independent of the per-outbound persona_drift_scores Haiku judge.
-- This catches whether she still holds her hard opinions
-- when challenged head-on. Table is brand-new (no ALTER ADD COLUMN). Indexes
-- live in the migration fn per MEMORY.md's schema-migration-ordering note so
-- the bootstrap pass stays index-free for fresh tables created via _SCHEMA.
CREATE TABLE IF NOT EXISTS drift_canary_answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    probe_key TEXT NOT NULL,
    asked_at TEXT NOT NULL,
    answer_text TEXT NOT NULL,
    verdict TEXT NOT NULL,        -- 'hold' | 'partial' | 'drift'
    reason TEXT,
    rubric_version TEXT NOT NULL DEFAULT 'v1',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Ghost-of-Future-Self letters: monthly LLM-composed letter written AS the
-- user 5 years from now (MIT Media Lab Future You project pattern). One
-- row per month_iso (YYYY-MM), persisted both here (queryable) and as a
-- markdown file under data/future_letters/ for human-readable durability.
-- Composer draws on receipts, episodes, character_thoughts, weekly
-- consolidations for the past 30 days. Brand-new table, no ALTER ADD
-- COLUMN, so the unique-constraint index lives inline.
CREATE TABLE IF NOT EXISTS future_letters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month_iso TEXT NOT NULL UNIQUE,    -- 'YYYY-MM'
    theme TEXT NOT NULL,                -- the "decision X" the letter reflects on
    body TEXT NOT NULL,                 -- the letter content
    sent_at TEXT,                       -- ISO ts when delivered via Telegram, null until then
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_future_letters_month
    ON future_letters(month_iso);

-- Decision log + Brier-style calibration. Capture: extract prediction
-- speech acts from chat into a row. Resolve: weekly job asks the user
-- about decisions whose resolve_by has passed. Mirror: rolling Brier
-- score surfaced in voice. Brand-new table, indexes inline per the
-- schema-migration-ordering memory rule.
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    statement TEXT NOT NULL,
    predicted_p REAL NOT NULL CHECK (predicted_p >= 0.0 AND predicted_p <= 1.0),
    resolve_by TEXT NOT NULL,
    outcome INTEGER,
    resolved_at TEXT,
    reasoning TEXT,
    asked_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_decisions_unresolved
    ON decisions(resolve_by) WHERE outcome IS NULL;
CREATE INDEX IF NOT EXISTS idx_decisions_resolved
    ON decisions(resolved_at) WHERE outcome IS NOT NULL;

CREATE TABLE IF NOT EXISTS schema_migrations (
    name TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL,
    checksum TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'run' CHECK(source IN ('run','backfill'))
);

CREATE TABLE IF NOT EXISTS media_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN ('text','photo','sticker','document')),
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sent','failed','aborted')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    processed_at TEXT,
    telegram_message_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_media_outbox_status ON media_outbox(status);
CREATE INDEX IF NOT EXISTS idx_media_outbox_kind_status ON media_outbox(kind, status);

-- Sprint 7F: sha256-hashed bearer token surface. Stores only the hash.
-- Plaintext token is returned once at create time and never persisted.
-- For simple bearer tokens created via oauth_token_create.
-- The full OAuth 2.1 dance uses the oauth_tokens table above.
CREATE TABLE IF NOT EXISTS oauth_token_hashes (
    token_hash TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    scopes TEXT
);
"""


# Ordered list of every migration name registered in the cascade.  Used by
# ``backfill_if_needed`` to stamp pre-7B databases on first boot.  Keep in sync
# with the ``run_once`` calls in ``_migrate_tasks_decay_columns``.
KNOWN_MIGRATIONS: list[str] = [
    "migrate_tasks_decay_columns",
    "migrate_approvals_defer_columns",
    # migrate_user_profile_to_peer_representation is intentionally excluded:
    # it is a data-conditional seeding op (not a DDL migration) and must run
    # on every boot to seed peer_representation from a legacy user_profile row
    # when one exists.  It is idempotent via its own early-return guard.
    "migrate_messages_telegram_message_id",
    "migrate_facts_bitemporal",
    "migrate_facts_recall_decay",
    "migrate_facts_attribution",
    "migrate_reminders_apple_columns",
    "migrate_drift_canary_indexes",
    "migrate_fact_relations_validity",
    "migrate_tool_calls",
    "migrate_proactive_events",
    "migrate_proactive_events_feedback",
    "migrate_proactive_events_chat_id",
    "migrate_proactive_events_status",
    "migrate_approvals_gatekeeper",
    "migrate_calendar_notifications",
    "migrate_entities_and_provenance",
    "migrate_messages_fts",
    "migrate_graph_outbox",
    "migrate_oauth_tokens_to_hash",
    "migrate_background_tasks_cancel",
    "migrate_media_events",
    "migrate_drop_persona_drift_probes",
    "migrate_drop_episode_summaries_and_fact_relations",
    "migrate_graph_outbox_drained_status",
    "migrate_sprint_a_tables",
    "migrate_fts_porter_tokenizer",
    "migrate_proactive_events_reason_contract",
    "migrate_phase_b_schema_tables",
]

# Process-level sentinel: schema setup + idempotent migrations only run on the
# first _conn() call per process. SQLite WAL covers cross-process safety; this
# sentinel eliminates per-connection PRAGMA table_info + bookkeeping reads from
# the steady-state path. Reset via ``_reset_schema_sentinel()`` in test fixtures.
_SCHEMA_INITIALIZED = False
_SCHEMA_LOCK = threading.Lock()

# Per-thread persistent connection pool. Keyed on _DB_PATH so test fixtures
# that swap _DB_PATH get a fresh connection on the next _conn() call.
_LOCAL = threading.local()


def _cfg_get(key: str, default: Any) -> Any:
    """Lazy config lookup. Falls back gracefully when agents package isn't
    importable (e.g. during standalone storage-only tests)."""
    try:
        from agents import config
        val = config.get(key)
        return val if val is not None else default
    except Exception:
        return default


def _reset_schema_sentinel() -> None:
    """Test helper — clears the process-level migration cache so test fixtures
    that swap ``_DB_PATH`` rerun migrations against the fresh per-test DB.
    Also closes and drops the per-thread cached connection so the next _conn()
    call opens a fresh connection against the new path."""
    global _SCHEMA_INITIALIZED
    _SCHEMA_INITIALIZED = False
    cached = getattr(_LOCAL, "conn", None)
    if cached is not None:
        try:
            cached.close()
        except Exception:
            pass
        try:
            del _LOCAL.conn
            del _LOCAL.path
        except AttributeError:
            pass


def _get_pooled_conn() -> sqlite3.Connection:
    """Return the per-thread cached SQLite connection. Lazy-init on first call
    per thread. Re-inits if _DB_PATH changed since the last call (covers test
    fixtures that swap the path between cases)."""
    cached = getattr(_LOCAL, "conn", None)
    cached_path = getattr(_LOCAL, "path", None)
    if cached is not None and cached_path == _DB_PATH:
        return cached
    if cached is not None:
        try:
            cached.close()
        except Exception:
            pass
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(_DB_PATH, check_same_thread=True)
    c.row_factory = sqlite3.Row
    c.enable_load_extension(True)
    sqlite_vec.load(c)
    c.enable_load_extension(False)
    try:
        c.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    busy_ms = int(_cfg_get("sqlite.busy_timeout_ms", 5000))
    c.execute(f"PRAGMA busy_timeout={busy_ms}")
    c.execute("PRAGMA synchronous=NORMAL")
    _ensure_schema(c)
    _LOCAL.conn = c
    _LOCAL.path = _DB_PATH
    return c


def _ensure_schema(conn: sqlite3.Connection) -> None:
    global _SCHEMA_INITIALIZED
    if _SCHEMA_INITIALIZED:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_INITIALIZED:
            return
        for stmt in _SCHEMA.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(stmt)
        backfill_if_needed(conn, KNOWN_MIGRATIONS)
        _migrate_tasks_decay_columns(conn)
        _SCHEMA_INITIALIZED = True


def _migrate_tasks_decay_columns(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add open-loop decay columns to ``tasks`` if missing.
    SQLite has no ``IF NOT EXISTS`` on ALTER COLUMN, so we sniff via PRAGMA.
    Each sub-migration is wrapped in ``run_once`` for ledger tracking."""
    def _body(c: sqlite3.Connection) -> None:
        existing = {row["name"] for row in c.execute("PRAGMA table_info(tasks)").fetchall()}
        if "importance" not in existing:
            c.execute("ALTER TABLE tasks ADD COLUMN importance INTEGER DEFAULT 5")
        if "mention_count" not in existing:
            c.execute("ALTER TABLE tasks ADD COLUMN mention_count INTEGER DEFAULT 0")
        if "last_mention_at" not in existing:
            c.execute("ALTER TABLE tasks ADD COLUMN last_mention_at TEXT")

    run_once(conn, "migrate_tasks_decay_columns", _body)
    run_once(conn, "migrate_approvals_defer_columns", _migrate_approvals_defer_columns)
    # Not wrapped in run_once: this is a data-conditional seeding op that must
    # run on every boot to seed peer_representation from a legacy user_profile
    # core_block when one exists.  Its own early-return guard makes it idempotent.
    _migrate_user_profile_to_peer_representation(conn)
    run_once(conn, "migrate_messages_telegram_message_id", _migrate_messages_telegram_message_id)
    run_once(conn, "migrate_facts_bitemporal", _migrate_facts_bitemporal)
    run_once(conn, "migrate_facts_recall_decay", _migrate_facts_recall_decay)
    run_once(conn, "migrate_facts_attribution", _migrate_facts_attribution)
    run_once(conn, "migrate_reminders_apple_columns", _migrate_reminders_apple_columns)
    run_once(conn, "migrate_drift_canary_indexes", _migrate_drift_canary_indexes)
    run_once(conn, "migrate_fact_relations_validity", _migrate_fact_relations_validity)
    run_once(conn, "migrate_tool_calls", _migrate_tool_calls)
    run_once(conn, "migrate_proactive_events", _migrate_proactive_events)
    run_once(conn, "migrate_proactive_events_feedback", _migrate_proactive_events_feedback)
    run_once(conn, "migrate_proactive_events_chat_id", _migrate_proactive_events_chat_id)
    run_once(conn, "migrate_proactive_events_status", _migrate_proactive_events_status)
    run_once(conn, "migrate_approvals_gatekeeper", _migrate_approvals_gatekeeper)
    run_once(conn, "migrate_calendar_notifications", _migrate_calendar_notifications)
    run_once(conn, "migrate_entities_and_provenance", _migrate_entities_and_provenance)
    run_once(conn, "migrate_messages_fts", _migrate_messages_fts)
    run_once(conn, "migrate_graph_outbox", _migrate_graph_outbox)
    run_once(conn, "migrate_oauth_tokens_to_hash", _migrate_oauth_tokens_to_hash)
    run_once(conn, "migrate_background_tasks_cancel", _migrate_background_tasks_cancel)
    run_once(conn, "migrate_media_events", _migrate_media_events)
    run_once(conn, "migrate_drop_persona_drift_probes", _migrate_drop_persona_drift_probes)
    run_once(conn, "migrate_drop_episode_summaries_and_fact_relations",
             _migrate_drop_episode_summaries_and_fact_relations)
    run_once(conn, "migrate_graph_outbox_drained_status", _migrate_graph_outbox_drained_status)
    run_once(conn, "migrate_sprint_a_tables", _migrate_sprint_a_tables)
    run_once(conn, "migrate_fts_porter_tokenizer", _migrate_fts_porter_tokenizer)
    run_once(conn, "migrate_proactive_events_reason_contract", _migrate_proactive_events_reason_contract)
    run_once(conn, "migrate_phase_b_schema_tables", _migrate_phase_b_schema_tables)
    # Commit any pending implicit transaction left open by migrations that
    # called conn.commit() internally (releasing SAVEPOINTs early) — the
    # ledger INSERT for those migrations stays in a Python-managed implicit
    # transaction that must be flushed before callers can write concurrently.
    conn.commit()


def _migrate_facts_bitemporal(conn: sqlite3.Connection) -> None:
    """T3.1: bi-temporal facts — add ``status``, ``superseded_by_fact_id``,
    and ``source``. The existing ``superseded_by`` column is preserved for
    backward compat; new writes populate both. Existing rows are backfilled to
    ``status='active'`` (or ``'invalid'`` if ``valid_to`` is already set)."""
    existing = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(facts)").fetchall()
    }
    if "status" not in existing:
        # SQLite ALTER ADD COLUMN with NOT NULL requires a constant default,
        # which ``'active'`` satisfies. Existing rows then get backfilled
        # below based on whether they were already invalidated.
        conn.execute(
            "ALTER TABLE facts ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
        )
        # Backfill: anything with valid_to set was already invalidated.
        conn.execute(
            "UPDATE facts SET status = 'invalid' "
            "WHERE valid_to IS NOT NULL AND superseded_by IS NULL"
        )
        conn.execute(
            "UPDATE facts SET status = 'superseded' "
            "WHERE superseded_by IS NOT NULL"
        )
    if "superseded_by_fact_id" not in existing:
        conn.execute(
            "ALTER TABLE facts ADD COLUMN superseded_by_fact_id INTEGER "
            "REFERENCES facts(id)"
        )
        # Backfill from the legacy ``superseded_by`` column.
        conn.execute(
            "UPDATE facts SET superseded_by_fact_id = superseded_by "
            "WHERE superseded_by IS NOT NULL"
        )
    if "source" not in existing:
        conn.execute("ALTER TABLE facts ADD COLUMN source TEXT")
    # Indexes — IF NOT EXISTS makes these idempotent.
    conn.execute("CREATE INDEX IF NOT EXISTS facts_status ON facts(status)")


def _migrate_fact_relations_validity(conn: sqlite3.Connection) -> None:
    """Bi-temporal fact_relations: when a fact transitions to 'superseded',
    every relation touching it gets stamped with valid_to + the new fact's
    id. Recall filters valid_to IS NOT NULL. Graphiti pattern (Zep,
    arxiv 2501.13956)."""
    existing = {
        row["name"]
        for row in conn.execute(
            "PRAGMA table_info(fact_relations)").fetchall()
    }
    if "valid_to" not in existing:
        conn.execute(
            "ALTER TABLE fact_relations ADD COLUMN valid_to TEXT"
        )
    if "invalidated_by_fact_id" not in existing:
        conn.execute(
            "ALTER TABLE fact_relations ADD COLUMN "
            "invalidated_by_fact_id INTEGER REFERENCES facts(id)"
        )
    # Per the schema-migration-ordering memory note: indexes for ALTER-added
    # columns live in the migration fn, never in _SCHEMA, because tests use
    # fresh DBs and _SCHEMA runs before migrations on those.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fact_relations_valid_to "
        "ON fact_relations(valid_to) WHERE valid_to IS NULL"
    )


def _migrate_tool_calls(conn: sqlite3.Connection) -> None:
    """Create the tool_calls telemetry table + its indexes. Lives in this
    migration fn (not _SCHEMA) so the indexes are co-located with the table
    DDL — the project's schema-migration-ordering rule applies to ALTER-added
    columns specifically, and following the same pattern here keeps the
    pattern uniform."""
    existing = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='tool_calls'"
    ).fetchall()}
    if "tool_calls" in existing:
        return
    conn.execute("""
        CREATE TABLE tool_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tool_id TEXT NOT NULL,
            started_at TEXT NOT NULL,
            duration_ms INTEGER NOT NULL,
            success INTEGER NOT NULL,
            error_class TEXT,
            output_size INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX idx_tool_calls_started_at ON tool_calls(started_at)")
    conn.execute("CREATE INDEX idx_tool_calls_tool_started ON tool_calls(tool_id, started_at)")


def _migrate_background_tasks_cancel(conn: sqlite3.Connection) -> None:
    """Add cancel_requested_at column to background_tasks for cooperative cancel."""
    existing = {row["name"] for row in conn.execute(
        "PRAGMA table_info(background_tasks)"
    ).fetchall()}
    if "cancel_requested_at" not in existing:
        conn.execute(
            "ALTER TABLE background_tasks ADD COLUMN cancel_requested_at TEXT"
        )


def _migrate_proactive_events(conn: sqlite3.Connection) -> None:
    """Create the proactive_events table + indexes for the engagement pipeline.
    Lives in a migration fn (not _SCHEMA) per the schema-migration-ordering
    convention so indexes are co-located with the table DDL."""
    existing = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='proactive_events'"
    ).fetchall()}
    if "proactive_events" in existing:
        return
    conn.execute("""
        CREATE TABLE proactive_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sent_at TEXT NOT NULL,
            source TEXT NOT NULL,
            pattern TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            telegram_message_id INTEGER
        )
    """)
    conn.execute("CREATE INDEX idx_proactive_events_sent_at ON proactive_events(sent_at)")
    conn.execute(
        "CREATE INDEX idx_proactive_events_source_sent ON proactive_events(source, sent_at)"
    )
    # Sprint 2's reaction-handler joins on telegram_message_id; partial index
    # keeps it small (NULL rows excluded) and avoids a full table scan.
    conn.execute(
        "CREATE INDEX idx_proactive_events_telegram_msg "
        "ON proactive_events(telegram_message_id) "
        "WHERE telegram_message_id IS NOT NULL"
    )


def _migrate_proactive_events_feedback(conn: sqlite3.Connection) -> None:
    """Phase D (Sprint 2): add reaction-feedback columns to proactive_events.

    Per MEMORY.md feedback_schema_migration_ordering: index refs to
    ALTER-added columns MUST live inside the migration fn — never in _SCHEMA.
    Tests use fresh DBs only so always launchctl-restart + tail err log after
    schema-changing merges."""
    cols = {r["name"] for r in conn.execute(
        "PRAGMA table_info(proactive_events)"
    ).fetchall()}
    if "thumbs_up" not in cols:
        conn.execute(
            "ALTER TABLE proactive_events ADD COLUMN "
            "thumbs_up INTEGER NOT NULL DEFAULT 0"
        )
    if "thumbs_down" not in cols:
        conn.execute(
            "ALTER TABLE proactive_events ADD COLUMN "
            "thumbs_down INTEGER NOT NULL DEFAULT 0"
        )
    if "silenced_within_1h" not in cols:
        conn.execute(
            "ALTER TABLE proactive_events ADD COLUMN "
            "silenced_within_1h INTEGER NOT NULL DEFAULT 0"
        )
    if "reaction_received_at" not in cols:
        conn.execute(
            "ALTER TABLE proactive_events ADD COLUMN "
            "reaction_received_at TEXT"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_proactive_events_feedback "
        "ON proactive_events(reaction_received_at) "
        "WHERE reaction_received_at IS NOT NULL"
    )


def _migrate_proactive_events_chat_id(conn: sqlite3.Connection) -> None:
    """Phase J: add chat_id to proactive_events for multi-user silence scoping."""
    cols = {r["name"] for r in conn.execute(
        "PRAGMA table_info(proactive_events)"
    ).fetchall()}
    if "chat_id" not in cols:
        conn.execute(
            "ALTER TABLE proactive_events ADD COLUMN chat_id INTEGER"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_proactive_events_chat_id_sent "
        "ON proactive_events(chat_id, sent_at)"
    )


def _migrate_proactive_events_status(conn: sqlite3.Connection) -> None:
    """Add status + aborted_reason + dedup_key columns to proactive_events for reservation audit.

    Index references the ALTER-added columns, so it must live inside the
    migration fn (not in _SCHEMA)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(proactive_events)").fetchall()}
    if "status" not in cols:
        conn.execute(
            "ALTER TABLE proactive_events ADD COLUMN status TEXT NOT NULL DEFAULT 'sent'"
        )
    if "aborted_reason" not in cols:
        conn.execute(
            "ALTER TABLE proactive_events ADD COLUMN aborted_reason TEXT"
        )
    if "dedup_key" not in cols:
        conn.execute("ALTER TABLE proactive_events ADD COLUMN dedup_key TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_proactive_events_status_sent "
        "ON proactive_events(status, sent_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_proactive_events_dedup "
        "ON proactive_events(source, dedup_key, sent_at) WHERE dedup_key IS NOT NULL"
    )


def _migrate_facts_recall_decay(conn: sqlite3.Connection) -> None:
    """T3.2: Ebbinghaus recall tracking — per-fact access timestamp + hit count."""
    existing = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(facts)").fetchall()
    }
    if "last_recalled_at" not in existing:
        conn.execute("ALTER TABLE facts ADD COLUMN last_recalled_at TEXT")
    if "recall_hit_count" not in existing:
        conn.execute(
            "ALTER TABLE facts ADD COLUMN recall_hit_count INTEGER NOT NULL DEFAULT 0"
        )


def _migrate_facts_attribution(conn: sqlite3.Connection) -> None:
    """Actor-aware attribution column on facts.

    Documented values (not enforced at DB level):
      user_stated         — user told Hikari directly
      user_observed       — inferred from user's actions, not stated
      user_corrected      — user replaced a prior fact via /memory correct
      hikari_inferred     — Hikari's own reflection extracted from chat
      subagent_extracted  — an explorer/research subagent surfaced it
      external_source     — came from a tool result (email, wiki, MCP)

    NULL = legacy/unknown. Recall scoring currently treats NULL as neutral.
    Pure-additive: no behavior change at recall today.
    """
    existing = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(facts)").fetchall()
    }
    if "attribution" not in existing:
        conn.execute("ALTER TABLE facts ADD COLUMN attribution TEXT")
    # No index — attribution is read alongside the fact row, not queried by.


def _migrate_drift_canary_indexes(conn: sqlite3.Connection) -> None:
    """Drift canary indexes. The table itself lives in _SCHEMA (brand-new, no
    ALTER ADD COLUMN), but per MEMORY.md ``feedback_schema_migration_ordering``
    we keep all indexes in the migration fn so the bootstrap pass never sees
    an index referencing a column that doesn't exist yet. Idempotent."""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS drift_canary_probe "
        "ON drift_canary_answers(probe_key, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS drift_canary_verdict "
        "ON drift_canary_answers(verdict)"
    )


def _migrate_reminders_apple_columns(conn: sqlite3.Connection) -> None:
    """Phase 11: add Apple Reminders mirror columns to ``reminders``."""
    existing = {row["name"] for row in conn.execute(
        "PRAGMA table_info(reminders)"
    ).fetchall()}
    try:
        if "apple_sync_pending" not in existing:
            conn.execute(
                "ALTER TABLE reminders ADD COLUMN "
                "apple_sync_pending INTEGER NOT NULL DEFAULT 0"
            )
    except Exception as exc:
        if "duplicate column" not in str(exc).lower():
            raise
    try:
        if "apple_event_id" not in existing:
            conn.execute(
                "ALTER TABLE reminders ADD COLUMN apple_event_id TEXT"
            )
    except Exception as exc:
        if "duplicate column" not in str(exc).lower():
            raise


def _migrate_messages_telegram_message_id(conn: sqlite3.Connection) -> None:
    """Phase 8: add `telegram_message_id` to `messages` so we can join user
    feedback (👍/👎 reactions on Hikari's outbound) back to the assistant row.

    Phase 13 (Stream C): also add `source` column so heuristics can
    distinguish ``chat`` (user-driven turn), ``proactive`` (heartbeat /
    reengage / calendar / reminder fire), and ``event`` (non-text user
    events such as photos / voice notes) rows.
    """
    existing = {row["name"] for row in conn.execute(
        "PRAGMA table_info(messages)"
    ).fetchall()}
    if "telegram_message_id" not in existing:
        conn.execute(
            "ALTER TABLE messages ADD COLUMN telegram_message_id INTEGER"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS messages_telegram_id "
            "ON messages(telegram_message_id) "
            "WHERE telegram_message_id IS NOT NULL"
        )
    if "source" not in existing:
        conn.execute(
            "ALTER TABLE messages ADD COLUMN source TEXT NOT NULL DEFAULT 'chat'"
        )


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


def _migrate_approvals_gatekeeper(conn: sqlite3.Connection) -> None:
    """Phase E (Sprint 2): add gatekeeper-specific columns to ``approvals``.

    Adds tool_use_id (backfilled from deferred_tool_use_id), deadline_iso,
    executed_at, result_summary, and gate_kind. Two partial unique indexes
    enforce the one-pending-per-chat and one-pending-per-use-id invariants.

    Per MEMORY.md feedback_schema_migration_ordering: indexes MUST live inside
    the migration fn, never in _SCHEMA, so fresh-DB bootstrap passes stay
    index-free for columns that don't exist yet.

    IMPORTANT: this migration contains a DML UPDATE statement. Unlike DDL
    (ALTER TABLE / CREATE INDEX), DML starts an implicit transaction in
    Python's sqlite3 module. We commit explicitly at the end so callers that
    invoke this outside a _conn() context manager (e.g. _ensure_schema) don't
    leave an open write transaction that blocks concurrent writers even in WAL
    mode (SQLITE_LOCKED bypasses busy_timeout).
    """
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(approvals)").fetchall()}
    needs_backfill = "tool_use_id" not in existing
    if needs_backfill:
        conn.execute("ALTER TABLE approvals ADD COLUMN tool_use_id TEXT")
    if "deadline_iso" not in existing:
        conn.execute("ALTER TABLE approvals ADD COLUMN deadline_iso TEXT")
    if "executed_at" not in existing:
        conn.execute("ALTER TABLE approvals ADD COLUMN executed_at TEXT")
    if "result_summary" not in existing:
        conn.execute("ALTER TABLE approvals ADD COLUMN result_summary TEXT")
    if "gate_kind" not in existing:
        conn.execute("ALTER TABLE approvals ADD COLUMN gate_kind TEXT")
    # Partial unique indexes — per the schema-migration-ordering memory note,
    # these live here (not in _SCHEMA) because they reference ALTER-added columns.
    # Gatekeeper-only: scope to gate_kind='gatekeeper' so the legacy defer path
    # (which always allowed multiple pending rows) is unaffected.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS approvals_one_pending_per_chat "
        "ON approvals(chat_id) WHERE status='pending' AND gate_kind='gatekeeper'"
    )
    # Drop then recreate so existing DBs get the corrected gate_kind clause
    # (the old index lacked it, which would allow the unique constraint to fire
    # across legacy defer rows that share a tool_use_id with a gatekeeper row).
    conn.execute("DROP INDEX IF EXISTS approvals_one_pending_per_use_id")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS approvals_one_pending_per_use_id "
        "ON approvals(tool_use_id) WHERE status='pending' "
        "AND tool_use_id IS NOT NULL AND gate_kind='gatekeeper'"
    )
    # Backfill AFTER DDL so the column exists and index is already built.
    if needs_backfill:
        conn.execute(
            "UPDATE approvals SET tool_use_id = deferred_tool_use_id "
            "WHERE tool_use_id IS NULL AND deferred_tool_use_id IS NOT NULL"
        )
    # Explicit commit: the DML UPDATE above starts an implicit transaction in
    # Python's sqlite3 module (isolation_level='', deferred). Without this,
    # _ensure_schema leaves the connection with an open write transaction that
    # causes SQLITE_LOCKED for concurrent writers even in WAL mode.
    conn.commit()


def _migrate_calendar_notifications(conn: sqlite3.Connection) -> None:
    """Replace runtime_state `calendar_notified_*` kv keys with a real table.
    Backfills existing keys; leaves the kv rows to age out separately."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS calendar_notifications ("
        "signature TEXT PRIMARY KEY, "
        "notified_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS calendar_notifications_at "
        "ON calendar_notifications(notified_at)"
    )
    # Backfill: scan runtime_state for legacy calendar_notified_ keys.
    prefix = "calendar_notified_"
    rows = conn.execute(
        "SELECT key FROM runtime_state WHERE key LIKE ?",
        (prefix + "%",),
    ).fetchall()
    for row in rows:
        sig = row["key"][len(prefix):]
        conn.execute(
            "INSERT OR IGNORE INTO calendar_notifications (signature) VALUES (?)",
            (sig,),
        )
    conn.commit()


def _migrate_graph_outbox_drained_status(conn: sqlite3.Connection) -> None:
    """Add 'drained' to graph_outbox.status CHECK constraint.

    SQLite cannot ALTER a CHECK in place; rebuild the table preserving rows.
    Idempotent — if the CHECK already includes 'drained', this is a no-op.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='graph_outbox'"
    ).fetchone()
    if row is None or "'drained'" in (row[0] or ""):
        return
    conn.execute("""
        CREATE TABLE graph_outbox_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_table TEXT NOT NULL,
            source_id INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','sent','failed','skipped','drained')),
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at INTEGER NOT NULL,
            processed_at INTEGER
        )
    """)
    conn.execute(
        "INSERT INTO graph_outbox_new "
        "(id, source_table, source_id, payload_json, status, attempts, last_error, created_at, processed_at) "
        "SELECT id, source_table, source_id, payload_json, status, attempts, last_error, created_at, processed_at "
        "FROM graph_outbox"
    )
    conn.execute("DROP TABLE graph_outbox")
    conn.execute("ALTER TABLE graph_outbox_new RENAME TO graph_outbox")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_graph_outbox_status_created "
        "ON graph_outbox(status, created_at)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_graph_outbox_source "
        "ON graph_outbox(source_table, source_id)"
    )


def _migrate_sprint_a_tables(conn: sqlite3.Connection) -> None:
    """Sprint A: new tables + ALTER columns + recurrence + emotional_register.

    Five new tables:
      - peer_insights: non-explicit observations from the dialectic extractor.
      - diary_entries: Hikari's first-person diary (one per day).
      - work_packets / work_packet_steps: typed durable plan for compound turns.
      - proactive_source_scores: per-source EMA + feedback counters.

    ALTER columns: sessions.emotional_register, episodes.stage_at_time,
    tool_calls.turn_id, reminders.recurrence_rule, messages.relationship_stage.

    Index/trigger references to ALTER-added columns live inside this fn, not
    in _SCHEMA (test DBs only re-run _SCHEMA, not migrations)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS peer_insights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observation TEXT NOT NULL,
            surface_score REAL NOT NULL DEFAULT 0.5,
            source TEXT,
            created_at INTEGER NOT NULL,
            surfaced_at INTEGER
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS peer_insights_score ON peer_insights(surface_score DESC, created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS peer_insights_unsurfaced ON peer_insights(surfaced_at, surface_score DESC) WHERE surfaced_at IS NULL")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS diary_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_date TEXT NOT NULL UNIQUE,
            body TEXT NOT NULL,
            sentiment TEXT,
            session_ids_json TEXT,
            created_at INTEGER NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS diary_entries_date ON diary_entries(entry_date DESC)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS work_packets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_turn_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'planning'
                CHECK (status IN ('planning','running','done','failed','cancelled','waiting')),
            summary TEXT,
            created_at INTEGER NOT NULL,
            finished_at INTEGER
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS work_packets_turn ON work_packets(user_turn_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS work_packets_status ON work_packets(status, created_at DESC)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS work_packet_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            packet_id INTEGER NOT NULL REFERENCES work_packets(id) ON DELETE CASCADE,
            step_index INTEGER NOT NULL,
            tool_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','running','done','waiting','failed','skipped','cancelled')),
            input_json TEXT,
            output_json TEXT,
            error TEXT,
            created_at INTEGER NOT NULL,
            finished_at INTEGER
        )""")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS work_packet_steps_unique ON work_packet_steps(packet_id, step_index)")
    conn.execute("CREATE INDEX IF NOT EXISTS work_packet_steps_status ON work_packet_steps(status, packet_id)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS proactive_source_scores (
            source TEXT PRIMARY KEY,
            ema REAL NOT NULL DEFAULT 0.5,
            n_pings INTEGER NOT NULL DEFAULT 0,
            n_thumbs_up INTEGER NOT NULL DEFAULT 0,
            n_thumbs_down INTEGER NOT NULL DEFAULT 0,
            last_update INTEGER NOT NULL
        )""")

    def _has_col(table: str, col: str) -> bool:
        return any(r["name"] == col for r in conn.execute(f"PRAGMA table_info({table})").fetchall())

    if not _has_col("session", "emotional_register"):
        conn.execute("ALTER TABLE session ADD COLUMN emotional_register TEXT")
    if not _has_col("episodes", "stage_at_time"):
        conn.execute("ALTER TABLE episodes ADD COLUMN stage_at_time INTEGER")
    if not _has_col("tool_calls", "turn_id"):
        conn.execute("ALTER TABLE tool_calls ADD COLUMN turn_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS tool_calls_turn_id ON tool_calls(turn_id) WHERE turn_id IS NOT NULL")
    if not _has_col("reminders", "recurrence_rule"):
        conn.execute("ALTER TABLE reminders ADD COLUMN recurrence_rule TEXT")
    if not _has_col("messages", "relationship_stage"):
        conn.execute("ALTER TABLE messages ADD COLUMN relationship_stage INTEGER")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS facts_recall_decay "
        "ON facts(last_recalled_at DESC, recall_hit_count DESC) "
        "WHERE valid_to IS NULL AND status='active'"
    )
    conn.commit()


def _migrate_fts_porter_tokenizer(conn: sqlite3.Connection) -> None:
    """Wave 2: migrate the ``fts`` virtual table from the default unicode61
    tokenizer to ``porter unicode61``, matching ``messages_fts``.

    Strategy: check current tokenizer via ``fts_config``; if already porter,
    return early.  Otherwise drop + recreate with porter tokenizer and
    repopulate from live facts and episodes rows.  Triggers and references to
    the old ``fts`` table are unaffected — the table name stays the same.

    Why this is safe: the ``fts`` virtual table is a non-content table (no
    ``content=`` clause pointing elsewhere), so all indexed text must be
    re-inserted.  We read from ``facts`` (active rows only) and ``episodes``
    to repopulate.  Concurrent writers are safe because this migration runs
    inside the single-writer boot-time migration pass.
    """
    # Check whether the fts table already uses porter tokenizer via fts_config.
    try:
        row = conn.execute(
            "SELECT v FROM fts_config WHERE k = 'tokenize'"
        ).fetchone()
        if row and "porter" in str(row[0]):
            return  # Already migrated.
    except sqlite3.OperationalError:
        # fts_config might not exist on very old DBs — proceed with migration.
        pass

    # Collect existing content before dropping.
    try:
        existing_rows = conn.execute(
            "SELECT content, kind, ref_id FROM fts"
        ).fetchall()
    except sqlite3.OperationalError:
        existing_rows = []

    # Drop the old table and shadow tables left by sqlite-fts5.
    conn.execute("DROP TABLE IF EXISTS fts")

    # Recreate with porter tokenizer.
    conn.execute("""
        CREATE VIRTUAL TABLE fts USING fts5(
            content,
            kind UNINDEXED,
            ref_id UNINDEXED,
            tokenize='porter unicode61'
        )
    """)

    # Repopulate: prefer live data from source tables to avoid reindexing
    # stale/deleted rows that may still be in the old fts (e.g. superseded facts).
    # Active facts only (status='active', valid_to IS NULL or future).
    try:
        conn.execute(
            "INSERT INTO fts (content, kind, ref_id) "
            "SELECT subject || ' ' || predicate || ' ' || object, 'fact', id "
            "FROM facts "
            "WHERE status = 'active' "
            "AND (valid_to IS NULL OR valid_to > datetime('now'))"
        )
    except sqlite3.OperationalError as exc:
        logger.warning("fts porter migration: fact repopulate failed: %s", exc)
        # Fall back to the pre-collected rows for facts.
        conn.executemany(
            "INSERT INTO fts (content, kind, ref_id) VALUES (?, ?, ?)",
            [
                (r[0], r[1], r[2]) for r in existing_rows if r[1] == "fact"
            ],
        )

    # Episodes — no status column, repopulate all.
    try:
        conn.execute(
            "INSERT INTO fts (content, kind, ref_id) "
            "SELECT summary, 'episode', id FROM episodes"
        )
    except sqlite3.OperationalError as exc:
        logger.warning("fts porter migration: episode repopulate failed: %s", exc)
        conn.executemany(
            "INSERT INTO fts (content, kind, ref_id) VALUES (?, ?, ?)",
            [
                (r[0], r[1], r[2]) for r in existing_rows if r[1] == "episode"
            ],
        )

    conn.commit()


def _migrate_proactive_events_reason_contract(conn: sqlite3.Connection) -> None:
    """Wave 3: add reason-contract columns to proactive_events.

    Columns (all nullable — existing rows default NULL):
      anchor         — real-world hook that triggered (gmail id, event id, file path, …)
      why_now        — short human-readable trigger-time explanation
      suggested_action — what the user might do in response
      confidence     — float 0..1 from the selector score
      controls_json  — JSON object of user-facing controls (snooze_hours, mute)
      data_checked_json — JSON array of data sources consulted (gmail, calendar, wiki, …)

    Index/trigger refs to ALTER-added columns must live inside this fn."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(proactive_events)").fetchall()}
    if "anchor" not in cols:
        conn.execute("ALTER TABLE proactive_events ADD COLUMN anchor TEXT")
    if "why_now" not in cols:
        conn.execute("ALTER TABLE proactive_events ADD COLUMN why_now TEXT")
    if "suggested_action" not in cols:
        conn.execute("ALTER TABLE proactive_events ADD COLUMN suggested_action TEXT")
    if "confidence" not in cols:
        conn.execute("ALTER TABLE proactive_events ADD COLUMN confidence REAL")
    if "controls_json" not in cols:
        conn.execute("ALTER TABLE proactive_events ADD COLUMN controls_json TEXT")
    if "data_checked_json" not in cols:
        conn.execute("ALTER TABLE proactive_events ADD COLUMN data_checked_json TEXT")


def _migrate_phase_b_schema_tables(conn: sqlite3.Connection) -> None:
    """Phase B: new tables, ALTER columns, and media_outbox CHECK widening.

    New tables (all brand-new — no ALTER ADD COLUMN for these):
      llm_costs            — per-turn token usage rollup
      voice_corrections    — FIFO drift-correction log
      belief_journal       — forward-looking belief capture with 90d resurface
      significant_events   — date-keyed anniversaries

    ALTER columns (indexes/triggers live INSIDE this fn per MEMORY.md rule):
      lexicon.first_seen_date  — anniversary callbacks on in-jokes
      facts.fact_category      — ACT-R decay tau selection hint
      tasks.research_intent    — background research worker flag

    CHECK widening:
      media_outbox.kind — add 'voice' to existing text/photo/sticker/document.
      Done via table-rebuild (SQLite can't ALTER a CHECK in place).
      Wrapped in a savepoint so production data is safe.
    """
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}

    # ------------------------------------------------------------------
    # llm_costs
    # ------------------------------------------------------------------
    if "llm_costs" not in tables:
        conn.execute("""
            CREATE TABLE llm_costs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                turn_id TEXT,
                model TEXT NOT NULL,
                path TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
                cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0.0
            )
        """)
    # Index refs to the new table live here (not _SCHEMA) — consistent with
    # the schema-migration-ordering rule even for brand-new tables.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_costs_ts ON llm_costs(ts)")

    # ------------------------------------------------------------------
    # voice_corrections
    # ------------------------------------------------------------------
    if "voice_corrections" not in tables:
        conn.execute("""
            CREATE TABLE voice_corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                correction_text TEXT NOT NULL,
                source_outbound_id INTEGER
            )
        """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_voice_corrections_ts_desc "
        "ON voice_corrections(ts DESC)"
    )

    # ------------------------------------------------------------------
    # belief_journal
    # ------------------------------------------------------------------
    if "belief_journal" not in tables:
        conn.execute("""
            CREATE TABLE belief_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stated_at TEXT NOT NULL,
                statement TEXT NOT NULL,
                claim_type TEXT NOT NULL
                    CHECK(claim_type IN ('factual', 'identity')),
                resurface_at TEXT NOT NULL,
                resolved_bool INTEGER NOT NULL DEFAULT 0,
                resolution_note TEXT
            )
        """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_belief_journal_resurface "
        "ON belief_journal(resurface_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_belief_journal_unresolved "
        "ON belief_journal(resolved_bool, resurface_at) WHERE resolved_bool = 0"
    )

    # ------------------------------------------------------------------
    # significant_events
    # ------------------------------------------------------------------
    if "significant_events" not in tables:
        conn.execute("""
            CREATE TABLE significant_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_date TEXT NOT NULL,
                summary TEXT NOT NULL,
                kind TEXT NOT NULL
                    CHECK(kind IN ('good', 'hard', 'funny', 'milestone')),
                created_at TEXT NOT NULL
            )
        """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_significant_events_date "
        "ON significant_events(event_date)"
    )

    # ------------------------------------------------------------------
    # ALTER columns
    # ------------------------------------------------------------------
    def _has_col(table: str, col: str) -> bool:
        return any(
            r["name"] == col
            for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
        )

    # lexicon.first_seen_date — backfill from created_at for existing rows
    if not _has_col("lexicon", "first_seen_date"):
        conn.execute("ALTER TABLE lexicon ADD COLUMN first_seen_date TEXT")
        conn.execute(
            "UPDATE lexicon SET first_seen_date = date(created_at) "
            "WHERE first_seen_date IS NULL AND created_at IS NOT NULL"
        )

    # facts.fact_category — nullable, no backfill needed
    if not _has_col("facts", "fact_category"):
        conn.execute("ALTER TABLE facts ADD COLUMN fact_category TEXT")

    # tasks.research_intent — boolean 0/1, default 0
    if not _has_col("tasks", "research_intent"):
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN research_intent INTEGER NOT NULL DEFAULT 0"
        )

    # ------------------------------------------------------------------
    # media_outbox CHECK widening: add 'voice'
    # SQLite cannot ALTER a CHECK; rebuild the table inside a savepoint.
    # ------------------------------------------------------------------
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='media_outbox'"
    ).fetchone()
    if row is not None and "'voice'" not in (row[0] or ""):
        conn.execute("SAVEPOINT phase_b_media_outbox")
        try:
            conn.execute("""
                CREATE TABLE media_outbox_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL
                        CHECK(kind IN ('text', 'photo', 'sticker', 'document', 'voice')),
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'sent', 'failed', 'aborted')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    processed_at TEXT,
                    telegram_message_id INTEGER
                )
            """)
            conn.execute(
                "INSERT INTO media_outbox_new "
                "(id, kind, idempotency_key, payload_json, status, attempts, "
                " last_error, created_at, processed_at, telegram_message_id) "
                "SELECT id, kind, idempotency_key, payload_json, status, attempts, "
                "       last_error, created_at, processed_at, telegram_message_id "
                "FROM media_outbox"
            )
            conn.execute("DROP TABLE media_outbox")
            conn.execute("ALTER TABLE media_outbox_new RENAME TO media_outbox")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_media_outbox_status "
                "ON media_outbox(status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_media_outbox_kind_status "
                "ON media_outbox(kind, status)"
            )
            conn.execute("RELEASE phase_b_media_outbox")
        except Exception:
            conn.execute("ROLLBACK TO phase_b_media_outbox")
            conn.execute("RELEASE phase_b_media_outbox")
            raise

    conn.commit()


_RUNTIME_STATE_KEYS_SPRINT_A: frozenset[str] = frozenset({
    "time_texture",
    "silenced_until_msg_id",
    "deferred_observations",
    "last_i_keep_thinking_at",
})


def _migrate_entities_and_provenance(conn: sqlite3.Connection) -> None:
    """5A: fact provenance columns + entities / entity_aliases / fact_entities tables."""
    # --- facts provenance: only new columns ---
    fcols = {r["name"] for r in conn.execute("PRAGMA table_info(facts)").fetchall()}
    if "source_span_hash" not in fcols:
        conn.execute("ALTER TABLE facts ADD COLUMN source_span_hash TEXT")
    if "recorded_at" not in fcols:
        conn.execute("ALTER TABLE facts ADD COLUMN recorded_at INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS facts_source_msg "
                 "ON facts(source_message_id) WHERE source_message_id IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS facts_recorded_at ON facts(recorded_at)")

    # --- entities ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL CHECK (kind IN ('person','project','place','app','topic')),
            canonical_name TEXT NOT NULL CHECK (length(canonical_name) BETWEEN 1 AND 200),
            created_at INTEGER NOT NULL,
            last_seen_at INTEGER NOT NULL,
            mention_count INTEGER NOT NULL DEFAULT 1
        )""")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS entities_kind_lname "
                 "ON entities(kind, lower(canonical_name))")
    conn.execute("CREATE INDEX IF NOT EXISTS entities_last_seen ON entities(last_seen_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS entities_kind ON entities(kind)")

    # --- entity_aliases ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entity_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            alias TEXT NOT NULL CHECK (length(alias) BETWEEN 1 AND 200),
            source TEXT NOT NULL DEFAULT 'auto' CHECK (source IN ('auto','user_stated'))
        )""")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS entity_aliases_eid_lalias "
                 "ON entity_aliases(entity_id, lower(alias))")
    conn.execute("CREATE INDEX IF NOT EXISTS entity_aliases_lalias ON entity_aliases(lower(alias))")

    # --- fact_entities ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fact_entities (
            fact_id INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
            entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            PRIMARY KEY (fact_id, entity_id)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS fact_entities_entity "
                 "ON fact_entities(entity_id, fact_id DESC)")
    conn.commit()


@contextmanager
def _conn():
    c = _get_pooled_conn()
    try:
        yield c
        c.commit()
    except Exception:
        try:
            c.rollback()
        except Exception:
            pass
        raise


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
    source: str | None = None,
    attribution: str | None = None,
    source_span_hash: str | None = None,
    recorded_at: int | None = None,
) -> int:
    """Insert a new fact. Returns row id. Caller is responsible for any
    contradiction/supersession logic — this function does NOT auto-supersede.

    Bi-temporal: ``valid_from`` is set to now, ``valid_to`` left NULL, and
    ``status`` defaults to ``'active'``. The optional ``source`` column is a
    free-text provenance tag (e.g. ``'user_message'``, ``'reflection'``).

    ``attribution`` is the structured provenance tag (one of:
    user_stated, user_observed, user_corrected, hikari_inferred,
    subagent_extracted, external_source) — see _migrate_facts_attribution
    for semantics.

    ``source_span_hash`` is a 16-hex-char SHA-256 of the source text span.
    ``recorded_at`` is the UTC epoch at which the fact was recorded; defaults
    to ``_utc_epoch()`` if not supplied."""
    if recorded_at is None:
        recorded_at = _utc_epoch()
    now = _now()
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO facts (subject, predicate, object, confidence, importance, "
            "valid_from, source_message_id, source, attribution, status, created_at, "
            "source_span_hash, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
            (subject, predicate, object_, confidence, importance, now,
             source_message_id, source, attribution, now,
             source_span_hash, recorded_at),
        )
        fact_id = cur.lastrowid
        c.execute(
            "INSERT INTO fts (content, kind, ref_id) VALUES (?, 'fact', ?)",
            (f"{subject} {predicate} {object_}", fact_id),
        )
        # Build outbox payload and insert in the same transaction as the fact.
        import json as _json
        _payload = {
            "v": 1,
            "name": f"fact_{fact_id}",
            "episode_body": f"{subject} {predicate} {object_}",
            "source": "text",
            "source_description": f"fact ({attribution or 'unknown'})",
            "group_id": "hikari_chat",
            "reference_time": datetime.now(UTC).isoformat(),
            "fact_id": fact_id,
        }
        graph_outbox_insert("facts", fact_id, _json.dumps(_payload), conn=c)
    return fact_id


def fact_insert(
    text: str,
    source: str | None = None,
    importance: int = 5,
    confidence: float = 0.9,
    attribution: str | None = None,
    source_message_id: int | None = None,
) -> int:
    """T3.1 — text-shaped fact insert. Thin wrapper over :func:`insert_fact`
    that takes a single free-text statement plus a provenance tag.

    The text is stored verbatim in the ``object`` column under a synthetic
    ``subject='user'``, ``predicate='note'`` so the FTS index still matches
    on the body. Returns the new row id.
    """
    body = (text or "").strip()
    if not body:
        raise ValueError("fact_insert: text is required")
    return insert_fact(
        subject="user",
        predicate="note",
        object_=body,
        importance=importance,
        confidence=confidence,
        source=source,
        attribution=attribution,
        source_message_id=source_message_id,
        source_span_hash=span_hash(body),
    )


# ---------- 5A: provenance + entity helpers ----------

def _utc_epoch() -> int:
    """Current UTC time as integer Unix epoch seconds."""
    return int(time.time())


def span_hash(text: str) -> str:
    """SHA-256 prefix (16 hex chars) of the stripped text — stable fingerprint for source spans."""
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()[:16]


def entity_upsert(kind: str, name: str) -> int:
    """Insert or update an entity row. Returns the entity id.

    Lookup order: canonical_name exact match → alias exact match → insert new.
    On any match, bumps last_seen_at and mention_count.
    Raises ValueError for bad kind or empty name.
    """
    kind = kind.strip().lower()
    if kind not in ("person", "project", "place", "app", "topic"):
        raise ValueError(f"entity_upsert: bad kind {kind!r}")
    nm = (name or "").strip()
    if not nm:
        raise ValueError("entity_upsert: name required")
    if len(nm) > 200:
        raise ValueError("entity_upsert: name exceeds 200 chars")
    now = _utc_epoch()
    with _conn() as c:
        row = c.execute(
            "SELECT id FROM entities WHERE kind=? AND lower(canonical_name)=lower(?)",
            (kind, nm)).fetchone()
        if not row:
            row = c.execute(
                "SELECT e.id FROM entities e JOIN entity_aliases a ON a.entity_id=e.id "
                "WHERE e.kind=? AND lower(a.alias)=lower(?)", (kind, nm)).fetchone()
        if row:
            eid = row["id"]
            c.execute("UPDATE entities SET last_seen_at=?, mention_count=mention_count+1 "
                      "WHERE id=?", (now, eid))
            return eid
        cur = c.execute(
            "INSERT INTO entities (kind, canonical_name, created_at, last_seen_at, mention_count) "
            "VALUES (?,?,?,?,1)", (kind, nm, now, now))
        return cur.lastrowid


def entity_alias_add(entity_id: int, alias: str, source: str = "auto") -> None:
    """Add an alias for an entity. Silently ignores blank aliases or duplicates."""
    a = (alias or "").strip()
    if not a:
        return
    if len(a) > 200:
        raise ValueError("entity_alias_add: alias exceeds 200 chars")
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO entity_aliases (entity_id, alias, source) "
                  "VALUES (?,?,?)", (entity_id, a, source))


# test-only: planned entity browser deferred — no production callers yet
def entity_get(entity_id: int) -> dict | None:
    """Fetch a single entity row by id. Returns None if not found."""
    with _conn() as c:
        r = c.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
    return dict(r) if r else None


# test-only: planned entity browser deferred — no production callers yet
def entity_search(kind: str | None, query: str, limit: int = 10) -> list[dict]:
    """Search entities by canonical_name or alias substring. Ordered by last_seen_at DESC."""
    q = f"%{(query or '').strip().lower()}%"
    sql = ("SELECT DISTINCT e.* FROM entities e "
           "LEFT JOIN entity_aliases a ON a.entity_id=e.id "
           "WHERE (lower(e.canonical_name) LIKE ? OR lower(a.alias) LIKE ?)")
    params: list = [q, q]
    if kind:
        sql += " AND e.kind = ?"
        params.append(kind)
    sql += " ORDER BY e.last_seen_at DESC LIMIT ?"
    params.append(int(limit))
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def fact_entities_link(fact_id: int, entity_ids: list[int]) -> None:
    """Link a list of entity ids to a fact. Idempotent (INSERT OR IGNORE)."""
    if not entity_ids:
        return
    with _conn() as c:
        c.executemany("INSERT OR IGNORE INTO fact_entities (fact_id, entity_id) VALUES (?,?)",
                      [(fact_id, eid) for eid in entity_ids])


def facts_by_entity(entity_id: int, limit: int = 20, status: str = "active") -> list[dict]:
    """Return facts linked to an entity, ordered by recorded_at DESC then id DESC."""
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT f.* FROM facts f JOIN fact_entities fe ON fe.fact_id=f.id "
            "WHERE fe.entity_id=? AND f.status=? "
            "ORDER BY COALESCE(f.recorded_at, 0) DESC, f.id DESC LIMIT ?",
            (entity_id, status, limit)).fetchall()]


def fact_provenance(fact_id: int) -> dict | None:
    """Return provenance fields for a fact joined with its source message row (if any)."""
    with _conn() as c:
        r = c.execute(
            "SELECT f.id AS fact_id, f.source_message_id, f.source_span_hash, "
            "f.recorded_at, f.attribution, f.source, "
            "m.id AS message_id, m.role, m.content, m.ts, m.telegram_message_id "
            "FROM facts f LEFT JOIN messages m ON m.id=f.source_message_id "
            "WHERE f.id=?", (fact_id,)).fetchone()
    return dict(r) if r else None


def active_facts_matching(subject: str, predicate: str) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM facts WHERE subject = ? AND predicate = ? AND valid_to IS NULL",
            (subject, predicate),
        ).fetchall()
    return [dict(r) for r in rows]


def supersede_fact(old_id: int, new_id: int, reason: str | None = None) -> None:
    """Mark old fact invalid (valid_to=now, superseded_by=new_id, status='superseded').

    Writes both the legacy ``superseded_by`` and the new ``superseded_by_fact_id``
    columns so older callers keep working while new readers use the explicit name.
    """
    with _conn() as c:
        c.execute(
            "UPDATE facts SET valid_to = ?, superseded_by = ?, "
            "superseded_by_fact_id = ?, status = 'superseded' WHERE id = ?",
            (_now(), new_id, new_id, old_id),
        )
        c.execute("DELETE FROM fts WHERE kind = 'fact' AND ref_id = ?", (old_id,))
        c.execute("DELETE FROM vec_facts WHERE id = ?", (old_id,))
        if reason:
            c.execute(
                "INSERT INTO character_thoughts (thought, created_at) VALUES (?, ?)",
                (f"superseded fact #{old_id} -> #{new_id}: {reason}", _now()),
            )


def invalidate_fact(fact_id: int, reason: str | None = None) -> None:
    """Mark a fact invalid without a superseding row (e.g. wrong fact entirely).

    Sets ``status='invalid'`` along with ``valid_to=now`` so bi-temporal readers
    can distinguish a flat invalidation from a supersession.
    """
    with _conn() as c:
        c.execute(
            "UPDATE facts SET valid_to = ?, status = 'invalid' WHERE id = ?",
            (_now(), fact_id),
        )
        c.execute("DELETE FROM fts WHERE kind = 'fact' AND ref_id = ?", (fact_id,))
        c.execute("DELETE FROM vec_facts WHERE id = ?", (fact_id,))
        if reason:
            c.execute(
                "INSERT INTO character_thoughts (thought, created_at) VALUES (?, ?)",
                (f"invalidated fact #{fact_id}: {reason}", _now()),
            )


def mark_fact_invalid(fact_id: int, superseded_by: int | None = None,
                      reason: str | None = None) -> None:
    """T3.1 — single entry point for the bi-temporal invalidation pattern.

    - Always sets ``valid_to = datetime('now')``.
    - If ``superseded_by`` is provided, sets ``status='superseded'`` AND
      ``superseded_by_fact_id=<id>`` (plus the legacy ``superseded_by`` column
      so prior consumers keep working).
    - Otherwise sets ``status='invalid'`` and leaves the superseded pointers NULL.

    Unlike :func:`invalidate_fact` and :func:`supersede_fact`, this preserves
    the row's FTS + vec entries so a historical ``recall`` (e.g. ``include_invalid``)
    can still surface them. Active-only recall is enforced by the ``valid_to``
    filter at the SQL layer.
    """
    fid = int(fact_id)
    if not fid:
        raise ValueError("mark_fact_invalid: fact_id is required")
    with _conn() as c:
        if superseded_by is not None:
            sup = int(superseded_by)
            c.execute(
                "UPDATE facts SET valid_to = datetime('now'), "
                "status = 'superseded', "
                "superseded_by_fact_id = ?, superseded_by = ? "
                "WHERE id = ?",
                (sup, sup, fid),
            )
        else:
            c.execute(
                "UPDATE facts SET valid_to = datetime('now'), "
                "status = 'invalid' WHERE id = ?",
                (fid,),
            )
        if reason:
            c.execute(
                "INSERT INTO character_thoughts (thought, created_at) VALUES (?, ?)",
                (f"mark_fact_invalid #{fid}: {reason}", _now()),
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


def facts_mark_recalled(fact_ids: list[int]) -> int:
    """T3.2 — stamp ``last_recalled_at = now`` and increment
    ``recall_hit_count`` for every id in ``fact_ids``. Returns the number
    of rows updated.

    Idempotent under concurrent calls because the increment is done in a
    single SQL statement (``recall_hit_count + 1``), not a Python-side
    read-modify-write. Empty input is a no-op.
    """
    ids = [int(i) for i in (fact_ids or []) if i]
    if not ids:
        return 0
    now = _now()
    placeholders = ",".join("?" * len(ids))
    with _conn() as c:
        cur = c.execute(
            f"UPDATE facts SET last_recalled_at = ?, "
            f"recall_hit_count = COALESCE(recall_hit_count, 0) + 1 "
            f"WHERE id IN ({placeholders})",
            (now, *ids),
        )
    return cur.rowcount or 0


# test-only: used only by tests/test_facts_recall_decay.py
def fact_backdate_created_at(fact_id: int, iso_ts: str) -> None:
    """Test/admin helper: forcibly rewrite the ``created_at``, ``valid_from``,
    and ``last_recalled_at`` timestamps for a fact. The recall-decay logic
    reads these to age out stale rows — tests need a way to inject "this
    fact is two months old" without ``time.sleep``.

    Production code should never call this. It exists in the public surface
    so the test suite doesn't have to monkey-patch ``_conn()``.
    """
    with _conn() as c:
        c.execute(
            "UPDATE facts SET created_at = ?, valid_from = ?, "
            "last_recalled_at = ? WHERE id = ?",
            (iso_ts, iso_ts, iso_ts, int(fact_id)),
        )


# ---------- messages ----------

def append_message(role: str, content: str, source: str = "chat") -> int:
    """Insert a row into ``messages``.

    Phase 13 (Stream C): the optional ``source`` discriminates ``chat``
    (real user turn / its assistant reply), ``proactive`` (heartbeat,
    reengage, calendar heartbeat, reminder fire), and ``event`` (non-text
    user input like photos / voice / document images). Defaults to
    ``chat`` for back-compat with every pre-stream-C caller.
    """
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO messages (role, content, ts, source) VALUES (?, ?, ?, ?)",
            (role, content, _now(), source),
        )
    return cur.lastrowid


def append_message_with_telegram_id(
    role: str,
    content: str,
    telegram_message_id: int,
    source: str = "chat",
) -> int:
    """Phase 13 (Stream C): append a message + stamp its Telegram outbound
    message_id in one insert.

    Used by ``_send_with_choreography`` after a successful Telegram send so
    the row commits with the final delivered text (codex P0 fix) AND the
    Telegram id needed for 👍/👎 feedback joins in the same transaction.
    Replaces the legacy two-step ``append_message`` + ``update_last_assistant_telegram_msg_id``.
    """
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO messages (role, content, ts, source, telegram_message_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (role, content, _now(), source, int(telegram_message_id)),
        )
    return cur.lastrowid


def recent_messages(limit: int = 20, *, exclude_ephemeral: bool = False) -> list[dict[str, Any]]:
    with _conn() as c:
        if exclude_ephemeral:
            rows = c.execute(
                "SELECT * FROM messages WHERE source NOT LIKE 'ephemeral:%' "
                "ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
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


# ---------- Phase 11: weekly sleep-time consolidation archive ----------

def weekly_consolidation_insert(
    week_ending: str,
    summary_text: str,
    episode_count: int,
) -> int:
    """Archive a completed week's consolidation summary. Returns the new row id.

    Called by the weekly sleep-time consolidation job (``run_weekly_consolidation``)
    immediately before it overwrites the ``weekly_consolidation`` core_block with
    the new week's text. The archive preserves the trail of past summaries so
    the user can drill back without bloating the always-on prompt.

    ``week_ending`` is the ISO date the snapshot represents (typically the
    Sunday the consolidation job ran). ``episode_count`` is informational —
    the number of underlying episodes/thoughts the summary was synthesized from.
    """
    body = (summary_text or "").strip()
    if not body:
        raise ValueError("weekly_consolidation_insert: summary_text is required")
    week = (week_ending or "").strip()
    if not week:
        raise ValueError("weekly_consolidation_insert: week_ending is required")
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO weekly_consolidations_archive "
            "(week_ending, summary_text, episode_count) VALUES (?, ?, ?)",
            (week, body, int(episode_count)),
        )
        return int(cur.lastrowid or 0)


def weekly_consolidations_recent(limit: int = 10) -> list[dict[str, Any]]:
    """Most-recent archived weekly consolidations, newest first."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, week_ending, summary_text, episode_count, created_at "
            "FROM weekly_consolidations_archive "
            "ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


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


# ---------- significant_events ----------

def significant_event_insert(
    *,
    event_date: str,
    summary: str,
    kind: str,
) -> int:
    """Insert into significant_events table. Idempotent on (event_date, summary[:80])."""
    with _conn() as c:
        existing = c.execute(
            "SELECT id FROM significant_events "
            "WHERE event_date = ? AND substr(summary, 1, 80) = ?",
            (event_date, summary[:80]),
        ).fetchone()
        if existing:
            return int(existing["id"])
        cur = c.execute(
            "INSERT INTO significant_events (event_date, summary, kind, created_at) "
            "VALUES (?, ?, ?, ?)",
            (event_date, summary, kind, _now()),
        )
        return cur.lastrowid


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


def observation_text(obs_id: int) -> str | None:
    """Return the summary text for an observation row, or None if not found."""
    with _conn() as c:
        row = c.execute(
            "SELECT summary FROM observations WHERE id = ?", (int(obs_id),)
        ).fetchone()
    return str(row["summary"]) if row and row["summary"] is not None else None


def noticing_text(not_id: int) -> str | None:
    """Return the summary text for a noticing row, or None if not found."""
    with _conn() as c:
        row = c.execute(
            "SELECT summary FROM noticings WHERE id = ?", (int(not_id),)
        ).fetchone()
    return str(row["summary"]) if row and row["summary"] is not None else None


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


# ---------- voice_corrections (Phase P reflexion loop) ----------


def voice_corrections_insert(*, correction_text: str, source_outbound_id: int | None) -> int:
    """Append a correction; trim to FIFO 10 in the same transaction."""
    text = (correction_text or "").strip()[:300]
    if not text:
        return 0
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO voice_corrections (ts, correction_text, source_outbound_id) "
            "VALUES (?, ?, ?)",
            (_now(), text, source_outbound_id),
        )
        new_id = cur.lastrowid
        c.execute(
            "DELETE FROM voice_corrections WHERE id IN ("
            "  SELECT id FROM voice_corrections ORDER BY id DESC LIMIT -1 OFFSET 10"
            ")"
        )
    return new_id


def voice_corrections_recent(limit: int = 3) -> list[dict]:
    """Most-recent first."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, ts, correction_text, source_outbound_id "
            "FROM voice_corrections ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- drift_canary_answers (weekly hard-opinion probe) ----------

def drift_canary_record(
    *,
    probe_key: str,
    asked_at: str,
    answer_text: str,
    verdict: str,
    reason: str | None,
    rubric_version: str = "v1",
) -> int:
    """Append one drift canary observation. Returns row id.

    ``verdict`` should be one of ``'hold' | 'partial' | 'drift'`` but the column
    isn't CHECK-constrained — callers may write ``'unknown'`` when the judge
    itself fails, so triage / reflection can distinguish a missing judgment
    from a real drift event.
    """
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO drift_canary_answers "
            "(probe_key, asked_at, answer_text, verdict, reason, rubric_version) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (probe_key, asked_at, answer_text, verdict, reason, rubric_version),
        )
    return cur.lastrowid


def drift_canary_recent(limit: int = 10) -> list[dict]:
    """Newest-first list of canary observations across all probes."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, probe_key, asked_at, answer_text, verdict, reason, "
            "rubric_version, created_at "
            "FROM drift_canary_answers "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def drift_canary_recent_by_probe(probe_key: str, limit: int = 5) -> list[dict]:
    """Newest-first list of canary observations for a single probe key."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, probe_key, asked_at, answer_text, verdict, reason, "
            "rubric_version, created_at "
            "FROM drift_canary_answers "
            "WHERE probe_key = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (probe_key, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


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


# ---------- user_feedback (Phase 8: 👍/👎 ground-truth) ----------

def update_last_assistant_telegram_msg_id(telegram_message_id: int) -> int | None:
    """Stamp the most-recent ``role='assistant'`` message row with its actual
    Telegram outbound ``message_id`` so we can later join 👍/👎 reactions
    back to the reply text. Returns the messages.id we updated (or None)."""
    with _conn() as c:
        row = c.execute(
            "SELECT id FROM messages WHERE role = 'assistant' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        c.execute(
            "UPDATE messages SET telegram_message_id = ? WHERE id = ?",
            (int(telegram_message_id), int(row["id"])),
        )
        return int(row["id"])


def feedback_record(telegram_message_id: int, rating: int) -> int:
    """Insert a 👍 (+1) or 👎 (-1) reaction. Returns the new row id."""
    if rating not in (-1, 1):
        raise ValueError(f"rating must be -1 or 1, got {rating!r}")
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO user_feedback (telegram_message_id, rating, created_at) "
            "VALUES (?, ?, ?)",
            (int(telegram_message_id), int(rating), _now()),
        )
    return int(cur.lastrowid or 0)


def feedback_recent(window_days: int = 7) -> list[dict]:
    """Reactions from the last ``window_days``, joined to the assistant message
    row for context."""
    from datetime import timedelta
    cutoff = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT uf.rating, uf.created_at, m.content "
            "FROM user_feedback uf "
            "LEFT JOIN messages m ON m.telegram_message_id = uf.telegram_message_id "
            "AND m.role = 'assistant' "
            "WHERE uf.created_at >= ? "
            "ORDER BY uf.created_at DESC",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def feedback_compare_to_drift(window_days: int = 7) -> dict:
    """Compare 👍/👎 against the drift judge's scores for the same messages.

    Returns ``{agree, disagree, examples}`` where:
      - ``agree`` counts cases where the judge said hikari (>=0.7) AND user
        gave +1, OR judge said drifting (<0.5) AND user gave -1.
      - ``disagree`` counts the inverse: judge said drifting + user gave +1,
        or judge said hikari + user gave -1.
      - ``examples`` is a short list of disagreement snippets so the user can
        eyeball whether the rubric needs tuning.
    """
    from datetime import timedelta
    cutoff = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
    agree = 0
    disagree = 0
    examples: list[str] = []
    # Review-H5 fix: a message may be sampled more than once by the drift
    # judge (no UNIQUE constraint on persona_drift_scores.message_id), and a
    # naive LEFT JOIN would count each drift sample as a separate vote for the
    # SAME feedback row, inflating agree/disagree counts. Pin to the MOST
    # RECENT drift sample per message via a correlated subquery.
    with _conn() as c:
        rows = c.execute(
            "SELECT uf.rating, m.id AS msg_id, m.content AS reply_text, "
            "  ("
            "    SELECT d.score FROM persona_drift_scores d "
            "    WHERE d.message_id = m.id "
            "    ORDER BY d.sampled_at DESC LIMIT 1"
            "  ) AS drift_score, "
            "  ("
            "    SELECT d.class_label FROM persona_drift_scores d "
            "    WHERE d.message_id = m.id "
            "    ORDER BY d.sampled_at DESC LIMIT 1"
            "  ) AS drift_class "
            "FROM user_feedback uf "
            "JOIN messages m ON m.telegram_message_id = uf.telegram_message_id "
            "  AND m.role = 'assistant' "
            "WHERE uf.created_at >= ? "
            "ORDER BY uf.created_at DESC",
            (cutoff,),
        ).fetchall()
    for r in rows:
        score = r["drift_score"]
        if score is None:
            continue
        rating = int(r["rating"])
        judge_hikari = float(score) >= 0.7
        judge_drift = float(score) < 0.5
        # Concordant: judge says hikari & user 👍, or judge says drifting & user 👎.
        if (judge_hikari and rating == 1) or (judge_drift and rating == -1):
            agree += 1
        elif (judge_hikari and rating == -1) or (judge_drift and rating == 1):
            disagree += 1
            snippet = (r["reply_text"] or "")[:80].replace("\n", " ")
            examples.append(
                f"judge={float(score):.2f} ({r['drift_class']}), user={rating:+d}: "
                f"{snippet!r}"
            )
    return {"agree": agree, "disagree": disagree, "examples": examples}


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


def runtime_increment(key: str, by: int = 1) -> int:
    """Atomic +N on a runtime_state integer key. Treats a missing or
    non-integer value as 0. Returns the new total.

    Uses a single SQL UPSERT with an arithmetic expression so it survives
    concurrent calls without a Python-side lock — fixes the read-modify-write
    race in counter bumps (Phase 9 review-F3).
    """
    with _conn() as c:
        c.execute(
            "INSERT INTO runtime_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = CAST("
            "  CAST(COALESCE(runtime_state.value, '0') AS INTEGER) + ? "
            "AS TEXT)",
            (key, str(int(by)), int(by)),
        )
        row = c.execute(
            "SELECT value FROM runtime_state WHERE key = ?", (key,)
        ).fetchone()
    try:
        return int(row["value"]) if row else int(by)
    except (ValueError, TypeError):
        return int(by)


def llm_costs_insert(
    *,
    turn_id: str | None,
    model: str,
    path: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int,
    cache_creation_input_tokens: int,
    cost_usd: float,
) -> int:
    """Insert a per-turn cost row into llm_costs (table from Phase A)."""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO llm_costs (ts, turn_id, model, path, "
            "input_tokens, output_tokens, "
            "cache_read_input_tokens, cache_creation_input_tokens, cost_usd) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (_now(), turn_id, model, path, input_tokens, output_tokens,
             cache_read_input_tokens, cache_creation_input_tokens, cost_usd),
        )
        return cur.lastrowid


def llm_costs_rollup(window_hours: int = 24) -> dict:
    """Return {n_rows, total_cost_usd, by_model: {model: cost}} for the last
    window_hours. Filters by ts >= now - window_hours."""
    from datetime import timedelta
    cutoff_iso = (datetime.now(UTC) - timedelta(hours=window_hours)).isoformat()
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd), 0.0) AS total "
            "FROM llm_costs WHERE ts >= ?",
            (cutoff_iso,),
        ).fetchone()
        per_model = c.execute(
            "SELECT model, COALESCE(SUM(cost_usd), 0.0) AS cost "
            "FROM llm_costs WHERE ts >= ? GROUP BY model ORDER BY cost DESC",
            (cutoff_iso,),
        ).fetchall()
    return {
        "n_rows": int(row["n"] or 0),
        "total_cost_usd": float(row["total"] or 0.0),
        "by_model": {r["model"]: float(r["cost"]) for r in per_model},
    }


# ---------- tool_calls telemetry ----------

def tool_calls_insert(
    *,
    tool_id: str,
    duration_ms: int,
    success: bool,
    error_class: str | None,
    output_size: int,
) -> int:
    """Insert one telemetry row. Returns the new row id."""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO tool_calls (tool_id, started_at, duration_ms, "
            "success, error_class, output_size) VALUES (?, ?, ?, ?, ?, ?)",
            (tool_id, _now(), int(duration_ms), 1 if success else 0,
             error_class, int(output_size)),
        )
    return int(cur.lastrowid or 0)


# ---------- proactive_events ----------

def proactive_event_insert(*, source: str, pattern: str, payload_json: str,
                           telegram_message_id: int | None = None,
                           chat_id: int | None = None,
                           status: str = "sent",
                           dedup_key: str | None = None,
                           anchor: str | None = None,
                           why_now: str | None = None,
                           suggested_action: str | None = None,
                           confidence: float | None = None,
                           controls_json: str | None = None,
                           data_checked_json: str | None = None) -> int:
    """Insert a row into proactive_events. Returns the new row id.

    ``status`` defaults to ``'sent'`` to backfill old call sites that haven't
    migrated to the reservation pattern yet. Pass ``'reserved'`` from
    ``reserve_and_send`` before the final gate runs.
    ``dedup_key`` is persisted to the dedup_key column for exact-match dedup.

    Reason-contract fields (Wave 3): anchor, why_now, suggested_action,
    confidence, controls_json, data_checked_json — all optional, default NULL.
    """
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO proactive_events "
            "(sent_at, source, pattern, payload_json, telegram_message_id, chat_id, "
            "status, dedup_key, anchor, why_now, suggested_action, confidence, "
            "controls_json, data_checked_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _now(), source, pattern, payload_json,
                telegram_message_id, chat_id, status, dedup_key,
                anchor, why_now, suggested_action, confidence,
                controls_json, data_checked_json,
            ),
        )
    return int(cur.lastrowid or 0)


def proactive_event_update_terminal(
    event_id: int,
    *,
    status: str,
    telegram_message_id: int | None = None,
    aborted_reason: str | None = None,
    payload_json: str | None = None,
    anchor: str | None = None,
    why_now: str | None = None,
    suggested_action: str | None = None,
    confidence: float | None = None,
    controls_json: str | None = None,
    data_checked_json: str | None = None,
) -> None:
    """Flip a reserved proactive_events row to a terminal state.
    Optionally update payload_json on the sent path so reservation rows
    can stay PII-minimal until commit.

    Reason-contract fields (Wave 3): when provided, the corresponding
    columns are set (COALESCE-patched so a NULL arg does not overwrite an
    existing value set at reserve time)."""
    with _conn() as c:
        sets = [
            "status = ?",
            "telegram_message_id = COALESCE(?, telegram_message_id)",
            "aborted_reason = ?",
            "anchor = COALESCE(?, anchor)",
            "why_now = COALESCE(?, why_now)",
            "suggested_action = COALESCE(?, suggested_action)",
            "confidence = COALESCE(?, confidence)",
            "controls_json = COALESCE(?, controls_json)",
            "data_checked_json = COALESCE(?, data_checked_json)",
        ]
        params: list = [
            status,
            telegram_message_id,
            aborted_reason,
            anchor,
            why_now,
            suggested_action,
            confidence,
            controls_json,
            data_checked_json,
        ]
        if payload_json is not None:
            sets.insert(1, "payload_json = ?")
            params.insert(1, payload_json)
        params.append(event_id)
        c.execute(
            f"UPDATE proactive_events SET {', '.join(sets)} WHERE id = ?",
            params,
        )


def proactive_event_dedup_hit(
    source: str, dedup_key: str, window_minutes: int
) -> bool:
    """Return True if a status='sent' proactive_events row exists for this
    source with the exact dedup_key within the window."""
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM proactive_events "
            "WHERE source = ? AND dedup_key = ? AND status = 'sent' "
            "AND sent_at >= datetime('now', ?) LIMIT 1",
            (source, dedup_key, f"-{window_minutes} minutes"),
        ).fetchone()
    return row is not None


def proactive_event_record_reaction(telegram_message_id: int, kind: str) -> int:
    """Record a 👍 or 👎 reaction on a proactive_events row.

    ``kind`` must be ``'up'`` or ``'down'``. Updates the matching row's
    thumbs_up/thumbs_down counter and stamps reaction_received_at on first
    reaction. Returns the number of rows updated (0 if no matching row)."""
    col = "thumbs_up" if kind == "up" else "thumbs_down"
    with _conn() as c:
        cur = c.execute(
            f"UPDATE proactive_events SET {col} = {col} + 1, "
            "reaction_received_at = COALESCE(reaction_received_at, ?) "
            "WHERE telegram_message_id = ?",
            (_now(), int(telegram_message_id)),
        )
    return int(cur.rowcount or 0)


def proactive_event_record_silence_window(
    chat_id: int | None = None,
    now_iso: str | None = None,
) -> int:
    """Flip silenced_within_1h=1 for rows sent in the last hour.

    When ``chat_id`` is provided, only rows matching that chat are updated.
    When ``chat_id`` is None, the filter is omitted (legacy single-user path).

    Called by /silence so the calibration loop can discount sources that tend
    to trigger silence commands.

    Returns the number of rows updated."""
    from datetime import timedelta
    cutoff = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    with _conn() as c:
        if chat_id is not None:
            cur = c.execute(
                "UPDATE proactive_events SET silenced_within_1h = 1 "
                "WHERE sent_at >= ? AND status = 'sent' AND chat_id = ?",
                (cutoff, int(chat_id)),
            )
        else:
            # XXX: legacy path — no chat_id; single-user bot only.
            cur = c.execute(
                "UPDATE proactive_events SET silenced_within_1h = 1 "
                "WHERE sent_at >= ? AND status = 'sent'",
                (cutoff,),
            )
    return int(cur.rowcount or 0)


# ---------- proactive engagement analytics ----------

def proactive_source_response_rates(days: int = 30) -> dict[str, float]:
    """Return thumbs-up response rate per source over the last N days.
    Rate = thumbs_up / (thumbs_up + thumbs_down), defaulting to 0.5 when
    no feedback exists. Used by the selector to weight source scores."""
    cutoff = (datetime.now(UTC) - __import__("datetime").timedelta(days=int(days))).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT source, SUM(thumbs_up) AS up, SUM(thumbs_down) AS dn "
            "FROM proactive_events "
            "WHERE sent_at >= ? AND status = 'sent' "
            "GROUP BY source",
            (cutoff,),
        ).fetchall()
    result: dict[str, float] = {}
    for r in rows:
        up = int(r["up"] or 0)
        dn = int(r["dn"] or 0)
        total = up + dn
        result[r["source"]] = (up / total) if total > 0 else 0.5
    return result


def proactive_last_send_per_source() -> dict[str, str]:
    """Return the ISO timestamp of the most recent send per source."""
    with _conn() as c:
        rows = c.execute(
            "SELECT source, MAX(sent_at) AS last_sent "
            "FROM proactive_events WHERE status = 'sent' "
            "GROUP BY source",
        ).fetchall()
    return {r["source"]: str(r["last_sent"]) for r in rows if r["last_sent"]}


def proactive_send_count_7d(source: str) -> int:
    """Return the number of sends for a given source in the last 7 days."""
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM proactive_events "
            "WHERE source = ? AND status = 'sent' AND sent_at >= datetime('now', '-7 days')",
            (str(source),),
        ).fetchone()
    return int(row["n"] or 0) if row else 0


# ---------- pruners ----------

def prune_messages_older_than_days(days: int) -> int:
    """Delete messages older than `days` from now. Returns rows deleted."""
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM messages WHERE ts < datetime('now', '-' || ? || ' days')",
            (int(days),),
        )
    return int(cur.rowcount or 0)


def prune_oauth_audit_log_older_than_days(days: int) -> int:
    """Delete oauth_audit_log rows older than `days`. Returns rows deleted."""
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM oauth_audit_log WHERE ts < datetime('now', '-' || ? || ' days')",
            (int(days),),
        )
    return int(cur.rowcount or 0)


# ---------- calendar_notifications ----------

def calendar_notification_set(signature: str) -> None:
    """Record that a calendar event was notified. Idempotent."""
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO calendar_notifications (signature) VALUES (?)",
            (signature,),
        )


def calendar_notification_exists(signature: str) -> bool:
    """Return True if the signature was previously recorded."""
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM calendar_notifications WHERE signature = ?",
            (signature,),
        ).fetchone()
    return row is not None


def prune_calendar_notifications_older_than_days(days: int) -> int:
    """Delete calendar_notifications rows older than `days`. Returns rows deleted."""
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM calendar_notifications "
            "WHERE notified_at < datetime('now', '-' || ? || ' days')",
            (int(days),),
        )
    return int(cur.rowcount or 0)


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
               "cost_usd", "tool_use_count", "cancel_requested_at"}
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


def bg_task_cancel_request(task_id: str) -> None:
    """Request cooperative cancellation of a queued or running task."""
    with _conn() as c:
        c.execute(
            "UPDATE background_tasks SET cancel_requested_at = ? "
            "WHERE task_id = ? AND status IN ('queued', 'running')",
            (_now(), task_id),
        )


def bg_task_cancel_requested(task_id: str) -> bool:
    """Return True if cancel_requested_at is set for this task."""
    with _conn() as c:
        row = c.execute(
            "SELECT cancel_requested_at FROM background_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    if row is None:
        return False
    return row["cancel_requested_at"] is not None


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


def approval_get(row_id: int) -> dict[str, Any] | None:
    """Return a single approvals row by primary key, or None."""
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM approvals WHERE id = ?",
            (int(row_id),),
        ).fetchone()
    return dict(row) if row else None


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
    """status: 'approved' | 'rejected' | 'timeout' | 'expired'."""
    with _conn() as c:
        c.execute(
            "UPDATE approvals SET status = ?, resolved_at = ? WHERE id = ?",
            (status, _now(), approval_id),
        )


def approval_create_gatekeeper(
    chat_id: int,
    tool_name: str,
    tool_use_id: str,
    args_json: str,
    summary: str,
    deadline_iso: str,
    gate_kind: str = "gatekeeper",
) -> int:
    """Phase E: write an approval row for the can_use_tool gatekeeper path.

    Unlike the legacy defer path, gatekeeper rows have:
      - tool_use_id populated from the SDK ToolPermissionContext
      - deadline_iso set by the caller
      - gate_kind = 'gatekeeper' (default) or caller-supplied value
      - tier = 2 (kept for schema compat with approval_pending_for)
    """
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO approvals "
            "(chat_id, tool_name, tier, summary, args_json, status, created_at, "
            " tool_use_id, deadline_iso, gate_kind) "
            "VALUES (?, ?, 2, ?, ?, 'pending', ?, ?, ?, ?)",
            (chat_id, tool_name, summary, args_json, _now(),
             tool_use_id, deadline_iso, gate_kind),
        )
    return int(cur.lastrowid or 0)


def approval_pending_by_use_id(tool_use_id: str) -> dict[str, Any] | None:
    """Return a pending approval row matching tool_use_id, or None."""
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM approvals "
            "WHERE tool_use_id = ? AND status = 'pending' "
            "LIMIT 1",
            (tool_use_id,),
        ).fetchone()
    return dict(row) if row else None


def approval_mark_executed(approval_id: int, result_summary: str) -> None:
    """Phase E: stamp executed_at + result_summary after the tool ran."""
    with _conn() as c:
        c.execute(
            "UPDATE approvals "
            "SET executed_at = ?, result_summary = ? "
            "WHERE id = ?",
            (_now(), result_summary, approval_id),
        )


def approval_expire_stale(cutoff_iso: str) -> int:
    """Phase E: mark pending gatekeeper rows older than cutoff_iso as 'timeout'.

    Returns the number of rows affected. Only targets gate_kind='gatekeeper'
    rows — legacy defer rows are not touched. Uses 'timeout' (not 'expired')
    because the approvals CHECK constraint only allows the legacy status values.
    """
    with _conn() as c:
        cur = c.execute(
            "UPDATE approvals SET status = 'timeout', resolved_at = ? "
            "WHERE status = 'pending' AND gate_kind = 'gatekeeper' "
            "AND created_at < ?",
            (_now(), cutoff_iso),
        )
    return int(cur.rowcount or 0)


def approvals_list_pending_gatekeeper() -> list[dict[str, Any]]:
    """Phase E: return all pending gatekeeper rows (for restart recovery)."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM approvals "
            "WHERE status = 'pending' AND gate_kind = 'gatekeeper' "
            "ORDER BY created_at",
        ).fetchall()
    return [dict(r) for r in rows]


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


# ---------- audit_log query helpers (Phase 6A cockpit) ----------

def audit_recent(limit: int = 20) -> list[dict]:
    """Return the most recent audit_log rows, newest first."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, ts, tool, args_json_redacted, result_summary, approved_by, "
            "hash_prev, hash_self "
            "FROM audit_log ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def tool_calls_recent(limit: int = 20) -> list[dict]:
    """Return the most recent tool_calls rows, newest first."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, tool_id, started_at, duration_ms, success, error_class, output_size "
            "FROM tool_calls ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def audit_by_id(row_id: int) -> dict | None:
    """Return a single audit_log row by primary key, or None."""
    with _conn() as c:
        row = c.execute(
            "SELECT id, ts, tool, args_json_redacted, result_summary, approved_by, "
            "hash_prev, hash_self "
            "FROM audit_log WHERE id = ?",
            (int(row_id),),
        ).fetchone()
    return dict(row) if row else None


def audit_by_tool(tool_pattern: str, limit: int = 20) -> list[dict]:
    """Return audit_log rows whose tool column matches a LIKE pattern."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, ts, tool, args_json_redacted, result_summary, approved_by "
            "FROM audit_log WHERE tool LIKE ? ORDER BY id DESC LIMIT ?",
            (tool_pattern, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def audit_tool_counts_7d() -> list[dict]:
    """Return (tool, count, last_ts) grouped by tool for the last 7 days, ordered by count desc."""
    with _conn() as c:
        rows = c.execute(
            "SELECT tool, COUNT(*) AS cnt, MAX(ts) AS last_ts "
            "FROM audit_log "
            "WHERE ts >= datetime('now', '-7 days') "
            "GROUP BY tool ORDER BY cnt DESC",
        ).fetchall()
    return [dict(r) for r in rows]


def audit_approvals_recent(limit: int = 20) -> list[dict]:
    """Return the most recent audit_log rows that were explicitly approved."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, ts, tool, args_json_redacted, result_summary, approved_by "
            "FROM audit_log WHERE approved_by IS NOT NULL "
            "ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def proactive_events_recent(days: int = 7, limit: int = 50) -> list[dict]:
    """Return recent proactive_events rows within the last N days."""
    cutoff = (datetime.now(UTC) - __import__("datetime").timedelta(days=int(days))).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT id, source, sent_at, payload_json, status, "
            "thumbs_up, thumbs_down, telegram_message_id "
            "FROM proactive_events "
            "WHERE sent_at >= ? ORDER BY id DESC LIMIT ?",
            (cutoff, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def proactive_event_by_id(event_id: int) -> dict | None:
    """Return a single proactive_events row by id, or None.

    Returns every column so callers (cockpit /proactive why) can read the
    Sprint A reason-contract fields (anchor, why_now, suggested_action,
    confidence, controls_json, data_checked_json).
    """
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM proactive_events WHERE id = ?",
            (int(event_id),),
        ).fetchone()
    return dict(row) if row else None


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


def vec_search_active_facts(query_vec: list[float], k: int = 30) -> list[dict[str, Any]]:
    """KNN search over vec_facts pre-filtered to status='active' facts.

    Wave 2: avoids wasting hydration work on already-invalidated/superseded rows.
    The subquery restricts the vec0 search space before the KNN pass so only
    active fact embeddings are considered — same result as post-fetch filtering
    but with fewer rows hydrated.

    Note: sqlite-vec vec0 supports id-range filtering in the WHERE clause
    alongside the vec MATCH predicate.  We use a ``WHERE id IN (subquery)``
    form that sqlite-vec evaluates as a pre-filter on the index.
    """
    if not query_vec or len(query_vec) != EMBEDDING_DIM:
        return []
    with _conn() as c:
        rows = c.execute(
            "SELECT v.id, v.distance FROM vec_facts v "
            "WHERE v.vec MATCH ? AND v.k = ? "
            "AND v.id IN ("
            "  SELECT id FROM facts "
            "  WHERE status = 'active' "
            "  AND (valid_to IS NULL OR valid_to > datetime('now'))"
            ") "
            "ORDER BY v.distance",
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
    import json as _json
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
            fid = cur.lastrowid
            c.execute(
                "INSERT INTO fts (content, kind, ref_id) VALUES (?, 'fact', ?)",
                (f"{r['subject']} {r['predicate']} {r['object']}", fid),
            )
            # Write outbox row in the same transaction.
            _payload = {
                "v": 1,
                "name": f"fact_{fid}",
                "episode_body": f"{r['subject']} {r['predicate']} {r['object']}",
                "source": "text",
                "source_description": "fact (unknown)",
                "group_id": "hikari_chat",
                "reference_time": datetime.now(UTC).isoformat(),
                "fact_id": fid,
            }
            graph_outbox_insert("facts", fid, _json.dumps(_payload), conn=c)
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


# ---------- Phase 10: reminders ----------

def reminder_insert(*, fire_at: str, text: str, lead_minutes: int = 0,
                    repeat: str | None = None,
                    recurrence_rule: str | None = None,
                    gcal_event_id: str | None = None,
                    gcal_sync_pending: bool = False,
                    apple_sync_pending: bool = False) -> int:
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO reminders "
            "(fire_at, lead_minutes, text, repeat, recurrence_rule, gcal_event_id, "
            "gcal_sync_pending, apple_sync_pending) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (fire_at, lead_minutes, text, repeat, recurrence_rule, gcal_event_id,
             1 if gcal_sync_pending else 0,
             1 if apple_sync_pending else 0),
        )
        return cur.lastrowid


def reminder_list(active_only: bool = True) -> list[dict[str, Any]]:
    with _conn() as conn:
        sql = "SELECT * FROM reminders"
        if active_only:
            sql += " WHERE status = 'active'"
        sql += " ORDER BY fire_at ASC"
        return [dict(r) for r in conn.execute(sql).fetchall()]


def reminder_due() -> list[dict[str, Any]]:
    """Rows whose effective fire time has passed and are still active."""
    with _conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM reminders "
            "WHERE status = 'active' "
            "AND datetime(fire_at, '-' || lead_minutes || ' minutes') <= datetime('now') "
            "ORDER BY fire_at ASC"
        ).fetchall()]


def reminder_mark_fired(reminder_id: int) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE reminders SET status = 'fired', fired_at = datetime('now') WHERE id = ?",
            (reminder_id,),
        )


def reminder_cancel(reminder_id: int) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE reminders SET status = 'cancelled' WHERE id = ?",
            (reminder_id,),
        )


def reminder_get(reminder_id: int) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        return dict(row) if row else None


def reminder_update_gcal_event(reminder_id: int, event_id: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE reminders SET gcal_event_id = ?, gcal_sync_pending = 0 WHERE id = ?",
            (event_id, reminder_id),
        )


def reminder_update_fire_at(reminder_id: int, new_fire_at: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE reminders SET fire_at = ? WHERE id = ?",
            (new_fire_at, reminder_id),
        )


def reminder_requeue_sync(reminder_id: int) -> None:
    """Re-flag both calendar sync columns so the next sync job updates the
    external event to the new fire time.  Only re-queues a flag when the
    existing event_id is non-null — a row that was never synced stays with
    its original pending state; a row that was successfully synced gets its
    flag flipped back to 1 so the sync job pushes the update.

    Idempotent: safe to call multiple times on the same row.
    """
    with _conn() as conn:
        conn.execute(
            "UPDATE reminders SET "
            "gcal_sync_pending = CASE"
            " WHEN gcal_event_id IS NOT NULL THEN 1"
            " ELSE gcal_sync_pending END, "
            "apple_sync_pending = CASE"
            " WHEN apple_event_id IS NOT NULL THEN 1"
            " ELSE apple_sync_pending END "
            "WHERE id = ?",
            (reminder_id,),
        )


def reminders_pending_gcal_sync(limit: int = 10) -> list[dict[str, Any]]:
    with _conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM reminders WHERE gcal_sync_pending = 1 AND status = 'active' "
            "ORDER BY created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()]


def reminder_update_apple_event(reminder_id: int, event_id: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE reminders SET apple_event_id = ?, apple_sync_pending = 0 WHERE id = ?",
            (event_id, reminder_id),
        )


def reminders_pending_apple_sync(limit: int = 10) -> list[dict[str, Any]]:
    with _conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM reminders WHERE apple_sync_pending = 1 AND status = 'active' "
            "ORDER BY created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()]


# ---------- accountability items ----------

def accountability_insert(
    reminder_id: int,
    follow_up_reminder_id: int,
    task_text: str,
) -> int:
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO accountability_items "
            "(reminder_id, follow_up_reminder_id, task_text) "
            "VALUES (?, ?, ?)",
            (reminder_id, follow_up_reminder_id, task_text),
        )
        return int(cur.lastrowid or 0)


def accountability_get(item_id: int) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM accountability_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        return dict(row) if row else None


def accountability_get_by_followup_id(follow_up_reminder_id: int) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM accountability_items WHERE follow_up_reminder_id = ?",
            (follow_up_reminder_id,),
        ).fetchone()
        return dict(row) if row else None


def accountability_resolve(item_id: int, outcome: int) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE accountability_items "
            "SET outcome = ?, resolved_at = datetime('now') "
            "WHERE id = ?",
            (outcome, item_id),
        )


def accountability_recent_unresolved(limit: int = 5) -> list[dict[str, Any]]:
    with _conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM accountability_items WHERE outcome IS NULL "
            "ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()]


def accountability_stats() -> dict[str, Any]:
    with _conn() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM accountability_items"
        ).fetchone()[0]
        resolved = conn.execute(
            "SELECT COUNT(*) FROM accountability_items WHERE outcome IS NOT NULL"
        ).fetchone()[0]
        did = conn.execute(
            "SELECT COUNT(*) FROM accountability_items WHERE outcome = 1"
        ).fetchone()[0]
        didnt = conn.execute(
            "SELECT COUNT(*) FROM accountability_items WHERE outcome = 0"
        ).fetchone()[0]
        did_rate = did / resolved if resolved > 0 else 0.0
        return {
            "total": total,
            "resolved": resolved,
            "did": did,
            "didnt": didnt,
            "did_rate": did_rate,
        }


def _is_darwin() -> bool:
    import sys
    return sys.platform == "darwin"


def accountability_create_atomic(
    when_iso_primary: str,
    when_iso_followup: str,
    task_text: str,
) -> tuple[int, int, int]:
    """Insert both reminders and the accountability row in a single transaction.

    Returns (reminder_id, follow_up_reminder_id, item_id).
    Rolls back entirely on any exception.
    """
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO reminders "
            "(fire_at, lead_minutes, text, repeat, recurrence_rule, gcal_event_id, "
            "gcal_sync_pending, apple_sync_pending) "
            "VALUES (?, 0, ?, NULL, NULL, NULL, 1, ?)",
            (when_iso_primary, task_text, 1 if _is_darwin() else 0),
        )
        rid = int(cur.lastrowid or 0)

        cur2 = conn.execute(
            "INSERT INTO reminders "
            "(fire_at, lead_minutes, text, repeat, recurrence_rule, gcal_event_id, "
            "gcal_sync_pending, apple_sync_pending) "
            "VALUES (?, 0, ?, NULL, NULL, NULL, 0, 0)",
            (when_iso_followup, task_text),
        )
        follow_rid = int(cur2.lastrowid or 0)

        cur3 = conn.execute(
            "INSERT INTO accountability_items "
            "(reminder_id, follow_up_reminder_id, task_text) "
            "VALUES (?, ?, ?)",
            (rid, follow_rid, task_text),
        )
        item_id = int(cur3.lastrowid or 0)

    return rid, follow_rid, item_id


# ---------- session scratch (Phase 11 — shared subagent memory) ----------

def scratch_cleanup_old(hours: int = 24) -> int:
    """Delete scratch entries older than N hours. Called by daily reflection."""
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM session_scratch "
            f"WHERE created_at < datetime('now', '-{int(hours)} hours')"
        )
        return cur.rowcount


# ---------- T7.2: photo locations (EXIF GPS) ----------

def photo_location_insert(
    lat: float, lon: float,
    label: str | None = None,
    taken_at: str | None = None,
) -> int:
    """Persist one EXIF-derived photo location. Returns the new row id.

    ``taken_at`` is the camera's EXIF DateTimeOriginal in whatever string form
    the caller extracted (we don't enforce ISO — Pillow returns its native
    format). ``label`` is whatever the reverse-geocoder produced (display_name
    or a synthesized address).
    """
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO photo_locations (lat, lon, label, taken_at) "
            "VALUES (?, ?, ?, ?)",
            (float(lat), float(lon), label, taken_at),
        )
        return int(cur.lastrowid or 0)


def photo_locations_recent(limit: int = 10) -> list[dict[str, Any]]:
    """Return most-recent photo locations, newest first."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, lat, lon, label, taken_at, received_at "
            "FROM photo_locations ORDER BY received_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def photo_location_delete(location_id: int) -> bool:
    """Delete a photo_locations row by id. Returns True if a row was deleted."""
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM photo_locations WHERE id = ?", (int(location_id),)
        )
        return cur.rowcount > 0


# ---------- Phase 14: OAuth 2.1 + PKCE + DCR (external MCP) ----------

def _oauth_random_token(byte_len: int = 32) -> str:
    """URL-safe random token. 32 bytes = 256 bits of entropy."""
    import secrets
    return secrets.token_urlsafe(byte_len)


def oauth_client_register(client_name: str | None,
                          redirect_uris: list[str]) -> dict[str, Any]:
    """RFC 7591 dynamic client registration. Public client (PKCE-only) —
    no client_secret issued. Returns the standard DCR response dict.

    Caller is responsible for redirect_uris validation (non-empty, well-formed).
    """
    import json
    if not redirect_uris:
        raise ValueError("oauth_client_register: redirect_uris is required")
    client_id = _oauth_random_token(16)
    with _conn() as c:
        c.execute(
            "INSERT INTO oauth_clients (client_id, client_name, redirect_uris) "
            "VALUES (?, ?, ?)",
            (client_id, client_name, json.dumps(list(redirect_uris))),
        )
    return {
        "client_id": client_id,
        "client_name": client_name,
        "redirect_uris": list(redirect_uris),
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }


def oauth_client_get(client_id: str) -> dict[str, Any] | None:
    """Look up a registered client by id. Returns the row with redirect_uris
    decoded, or None if not found."""
    import json
    with _conn() as c:
        row = c.execute(
            "SELECT client_id, client_name, redirect_uris, created_at, "
            "last_used_at FROM oauth_clients WHERE client_id = ?",
            (client_id,),
        ).fetchone()
    if not row:
        return None
    out = dict(row)
    try:
        out["redirect_uris"] = json.loads(out["redirect_uris"])
    except (TypeError, ValueError):
        out["redirect_uris"] = []
    return out


def oauth_client_touch(client_id: str) -> None:
    """Bump ``last_used_at`` on a client. Best-effort."""
    with _conn() as c:
        c.execute(
            "UPDATE oauth_clients SET last_used_at = ? WHERE client_id = ?",
            (_now(), client_id),
        )


def oauth_code_mint(client_id: str, redirect_uri: str,
                    code_challenge: str, code_challenge_method: str,
                    scope: str | None = None,
                    ttl_seconds: int = 600) -> str:
    """Mint a single-use authorization code bound to a PKCE challenge."""
    from datetime import timedelta
    code = _oauth_random_token(32)
    expires_at = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat()
    with _conn() as c:
        c.execute(
            "INSERT INTO oauth_codes (code, client_id, redirect_uri, "
            "code_challenge, code_challenge_method, scope, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (code, client_id, redirect_uri, code_challenge,
             code_challenge_method, scope, expires_at),
        )
    return code


def oauth_code_consume(code: str, code_verifier: str) -> dict[str, Any] | None:
    """Verify the PKCE S256 challenge and atomically consume the code.

    Returns the code row on success. Returns None on any failure path:
    unknown code, already consumed, expired, verifier mismatch, or a race
    where another caller consumed the row first. Errors are not distinguished
    so callers can't fingerprint why a given code failed.
    """
    import base64
    import hashlib
    import secrets as _secrets
    with _conn() as c:
        row = c.execute(
            "SELECT code, client_id, redirect_uri, code_challenge, "
            "code_challenge_method, scope, expires_at, consumed_at "
            "FROM oauth_codes WHERE code = ?",
            (code,),
        ).fetchone()
        if not row or row["consumed_at"] or row["expires_at"] < _now():
            return None
        if row["code_challenge_method"] != "S256":
            return None
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        if not _secrets.compare_digest(computed, row["code_challenge"]):
            return None
        cur = c.execute(
            "UPDATE oauth_codes SET consumed_at = ? "
            "WHERE code = ? AND consumed_at IS NULL",
            (_now(), code),
        )
        if cur.rowcount == 0:
            return None
    return dict(row)


def oauth_token_mint(client_id: str, token_type: str,
                     parent_token: str | None = None,
                     scope: str | None = None,
                     ttl_seconds: int = 3600) -> str:
    """Issue an opaque access or refresh token. Returns the token string."""
    from datetime import timedelta
    if token_type not in ("access", "refresh"):
        raise ValueError(f"invalid token_type: {token_type!r}")
    tok = _oauth_random_token(32)
    expires_at = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat()
    with _conn() as c:
        c.execute(
            "INSERT INTO oauth_tokens (token, client_id, token_type, "
            "parent_token, scope, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (tok, client_id, token_type, parent_token, scope, expires_at),
        )
    return tok


def oauth_token_validate(token: str) -> dict[str, Any] | None:
    """Validate a bearer token by sha256 hash lookup against oauth_token_hashes.

    Returns metadata dict ``{owner, expires_at, scopes}`` on success, or None
    if the token is unknown or expired. Uses hmac.compare_digest for
    constant-time confirmation of the hash match.

    NOTE: This validates tokens created via ``oauth_token_create``. For the
    full OAuth 2.1 dance tokens (oauth_tokens table) use
    ``_oauth2_token_validate``.
    """
    import hmac
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    row = _get_pooled_conn().execute(
        "SELECT token_hash, owner, expires_at, scopes FROM oauth_token_hashes "
        "WHERE token_hash = ?",
        (token_hash,),
    ).fetchone()
    if row is None:
        return None
    # Constant-time PK confirmation (defensive against timing side-channels if
    # the query planner ever takes a different path).
    if not hmac.compare_digest(row["token_hash"], token_hash):
        return None
    # Check expiry — None means no expiry set (non-expiring token).
    if row["expires_at"] is not None and row["expires_at"] < _now():
        return None
    return {"owner": row["owner"], "expires_at": row["expires_at"],
            "scopes": row["scopes"]}


def _oauth2_token_validate(token: str) -> dict[str, Any] | None:
    """Validate a full OAuth 2.1 access token from the oauth_tokens table.
    Returns the row if active (unexpired AND unrevoked), else None.
    Bumps ``last_used_at`` on the validated row."""
    with _conn() as c:
        row = c.execute(
            "SELECT token, client_id, token_type, parent_token, scope, "
            "expires_at, revoked_at FROM oauth_tokens WHERE token = ?",
            (token,),
        ).fetchone()
        if not row or row["revoked_at"] or row["expires_at"] < _now():
            return None
        c.execute(
            "UPDATE oauth_tokens SET last_used_at = ? WHERE token = ?",
            (_now(), token),
        )
    return dict(row)


def oauth_token_create(
    owner: str,
    scopes: str | None = None,
    expires_at: str | None = None,
) -> str:
    """Generate a fresh bearer token, hash it, insert the hash, and return
    the plaintext token. This is the ONLY time the plaintext is visible —
    the caller MUST save it immediately.

    The token is a URL-safe random 32-byte value (256 bits of entropy). Only
    the sha256 hash is stored; the plaintext is never persisted.
    """
    import secrets as _secrets
    token = _secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with _conn() as c:
        c.execute(
            "INSERT INTO oauth_token_hashes(token_hash, owner, created_at, expires_at, scopes) "
            "VALUES (?, ?, ?, ?, ?)",
            (token_hash, owner, _now(), expires_at, scopes),
        )
    return token  # operator MUST save this; it is never recoverable


def oauth_token_revoke(token: str) -> bool:
    """Revoke a hashed bearer token from oauth_token_hashes by hash.

    Returns True if a row was deleted, False if no matching token was found.
    For full OAuth 2.1 token revocation, use ``oauth_token_revoke_family`` or
    the direct ``oauth_tokens`` table operations.
    """
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM oauth_token_hashes WHERE token_hash = ?",
            (token_hash,),
        )
    return cur.rowcount > 0


def oauth_token_consume_refresh(token: str, client_id: str) -> dict[str, Any] | None:
    """Atomically validate + revoke a refresh token in one transaction.

    Used by the /token refresh grant. Returns the original row on success
    (which the caller then uses to mint a new access+refresh pair under the
    same scope). Returns None if the token doesn't exist, is the wrong type,
    is expired, was already revoked, doesn't belong to ``client_id``, OR if
    a concurrent request beat us to the revoke. The single-transaction
    consume-then-revoke prevents two parallel rotation requests from each
    minting their own live token chain off the same parent."""
    with _conn() as c:
        row = c.execute(
            "SELECT token, client_id, token_type, parent_token, scope, "
            "expires_at, revoked_at FROM oauth_tokens WHERE token = ?",
            (token,),
        ).fetchone()
        if (
            not row
            or row["revoked_at"]
            or row["expires_at"] < _now()
            or row["token_type"] != "refresh"
            or row["client_id"] != client_id
        ):
            return None
        cur = c.execute(
            "UPDATE oauth_tokens SET revoked_at = ? "
            "WHERE token = ? AND revoked_at IS NULL",
            (_now(), token),
        )
        if cur.rowcount == 0:
            return None
        c.execute(
            "UPDATE oauth_tokens SET revoked_at = ? "
            "WHERE parent_token = ? AND revoked_at IS NULL",
            (_now(), token),
        )
    return dict(row)


def oauth_token_revoke_family(parent_token: str) -> int:
    """Revoke a refresh token and every access token descended from it.

    Called during refresh rotation: when the client exchanges refresh R1 for
    a new pair (A2, R2), every token whose ``parent_token == R1`` plus R1
    itself is revoked. Returns rows updated."""
    with _conn() as c:
        cur = c.execute(
            "UPDATE oauth_tokens SET revoked_at = ? "
            "WHERE (token = ? OR parent_token = ?) "
            "AND revoked_at IS NULL",
            (_now(), parent_token, parent_token),
        )
        return int(cur.rowcount or 0)


def oauth_audit(event_type: str, client_id: str | None = None,
                ip: str | None = None,
                details: dict[str, Any] | None = None) -> int:
    """Append an OAuth audit row. Separate ledger from the hash-chained
    ``audit_log`` — OAuth events are high-frequency and don't need the
    forensic chain. ``details_json`` is truncated to 2KB."""
    import json
    payload = json.dumps(details or {}, default=str, ensure_ascii=False)[:2000]
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO oauth_audit_log "
            "(event_type, client_id, ip, details_json) VALUES (?, ?, ?, ?)",
            (event_type, client_id, ip, payload),
        )
        return int(cur.lastrowid or 0)


def oauth_cleanup_expired(revoked_retention_days: int = 30) -> int:
    """Sweep expired oauth_codes (consumed or past TTL), expired/old-revoked
    oauth_tokens, and expired oauth_token_hashes. Called from daily reflection.
    Returns total rows deleted.

    Comparisons use Python isoformat strings, not ``datetime('now')`` —
    expires_at / revoked_at are written via ``_now()`` (Python isoformat with
    'T' separator + tz offset), and SQLite's ``datetime('now')`` produces a
    space-separated, tz-naive string that does NOT sort lexicographically
    against ours."""
    from datetime import timedelta
    now_iso = _now()
    revoked_cutoff = (datetime.now(UTC) - timedelta(
        days=int(revoked_retention_days))).isoformat()
    with _conn() as c:
        n1 = c.execute(
            "DELETE FROM oauth_codes "
            "WHERE expires_at < ? OR consumed_at IS NOT NULL",
            (now_iso,),
        ).rowcount
        n2 = c.execute(
            "DELETE FROM oauth_tokens "
            "WHERE expires_at < ? "
            "OR (revoked_at IS NOT NULL AND revoked_at < ?)",
            (now_iso, revoked_cutoff),
        ).rowcount
        n3 = c.execute(
            "DELETE FROM oauth_token_hashes "
            "WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now_iso,),
        ).rowcount
    return int(n1 or 0) + int(n2 or 0) + int(n3 or 0)


# ---------- future_letters (Ghost-of-Future-Self monthly letter) ----------

def future_letter_insert(month_iso: str, theme: str, body: str) -> int:
    """Insert a composed letter for ``month_iso`` (``YYYY-MM``). Returns the
    new row id. Raises ``sqlite3.IntegrityError`` on duplicate month (the
    UNIQUE constraint catches double-fires)."""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO future_letters (month_iso, theme, body) "
            "VALUES (?, ?, ?)",
            (month_iso, theme, body),
        )
    return int(cur.lastrowid or 0)


def future_letter_get(month_iso: str) -> dict[str, Any] | None:
    """Return the letter row for the given ``YYYY-MM`` month, or None."""
    with _conn() as c:
        row = c.execute(
            "SELECT id, month_iso, theme, body, sent_at, created_at "
            "FROM future_letters WHERE month_iso = ?",
            (month_iso,),
        ).fetchone()
    return dict(row) if row else None


def future_letter_mark_sent(month_iso: str) -> None:
    """Stamp ``sent_at`` with the current time once Telegram delivery succeeds.
    Idempotent — overwrites a prior stamp if called again."""
    with _conn() as c:
        c.execute(
            "UPDATE future_letters SET sent_at = ? WHERE month_iso = ?",
            (_now(), month_iso),
        )


# ---------- decisions (calibration log) ----------

def decision_insert(statement: str, predicted_p: float, resolve_by: str,
                    reasoning: str | None = None) -> int:
    """Insert a captured prediction. resolve_by is ISO date or ISO datetime."""
    p = max(0.0, min(1.0, float(predicted_p)))
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO decisions (statement, predicted_p, resolve_by, "
            "reasoning) VALUES (?, ?, ?, ?)",
            (statement, p, resolve_by, reasoning),
        )
    return int(cur.lastrowid or 0)


def decision_resolve(decision_id: int, outcome: int) -> None:
    """Mark a decision resolved. outcome must be 0 or 1.

    Immutable: refuses to overwrite an existing outcome with a different
    value. Same-value re-resolve is a silent no-op (true idempotency).
    Every state-changing call writes an audit_log row so the calibration
    ledger has a forensic trail against prompt-injected resolves.
    """
    import json
    if outcome not in (0, 1):
        raise ValueError("outcome must be 0 or 1")
    did = int(decision_id)
    with _conn() as c:
        row = c.execute(
            "SELECT outcome FROM decisions WHERE id = ?", (did,)
        ).fetchone()
        if row is None:
            raise ValueError(f"decision {did} not found")
        if row["outcome"] is not None:
            if int(row["outcome"]) != int(outcome):
                raise ValueError(
                    f"decision {did} already resolved as {row['outcome']}; "
                    f"refusing to overwrite with {outcome}"
                )
            return
        c.execute(
            "UPDATE decisions SET outcome = ?, resolved_at = ? "
            "WHERE id = ? AND outcome IS NULL",
            (int(outcome), _now(), did),
        )
    audit_append(
        tool="decision_resolve",
        args_json_redacted=json.dumps(
            {"decision_id": did, "outcome": int(outcome)}
        ),
        result_summary="resolved",
        approved_by="owner",
    )


def decisions_unresolved_due(limit: int = 5,
                             cooldown_days: int = 14) -> list[dict[str, Any]]:
    """Decisions whose resolve_by has passed and outcome is still null,
    oldest first. Skips rows asked about within the cooldown window."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, statement, predicted_p, resolve_by, asked_at "
            "FROM decisions "
            "WHERE outcome IS NULL "
            "AND resolve_by <= date('now') "
            "AND (asked_at IS NULL "
            "     OR asked_at < datetime('now', '-' || ? || ' days')) "
            "ORDER BY resolve_by ASC LIMIT ?",
            (int(cooldown_days), int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def decisions_unresolved_overdue_count(cooldown_days: int = 14) -> int:
    """Count of overdue-unresolved decisions, for the inject_memory mirror."""
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM decisions "
            "WHERE outcome IS NULL "
            "AND resolve_by <= date('now') "
            "AND (asked_at IS NULL "
            "     OR asked_at < datetime('now', '-' || ? || ' days'))",
            (int(cooldown_days),),
        ).fetchone()
    return int(row["n"] or 0)


def decision_mark_asked(decision_id: int) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE decisions SET asked_at = ? WHERE id = ?",
            (_now(), int(decision_id)),
        )


def decision_brier_score(window_days: int = 90) -> dict[str, Any]:
    """Return ``{n, brier, mean_predicted, mean_outcome}`` over decisions
    resolved in the last window_days, or ``{n: 0}`` if none."""
    with _conn() as c:
        rows = c.execute(
            "SELECT predicted_p, outcome FROM decisions "
            "WHERE outcome IS NOT NULL "
            "AND resolved_at >= datetime('now', '-' || ? || ' days')",
            (int(window_days),),
        ).fetchall()
    n = len(rows)
    if n == 0:
        return {"n": 0}
    brier = sum((float(r["predicted_p"]) - int(r["outcome"])) ** 2
                for r in rows) / n
    mean_p = sum(float(r["predicted_p"]) for r in rows) / n
    mean_o = sum(int(r["outcome"]) for r in rows) / n
    return {
        "n": n, "brier": round(brier, 4),
        "mean_predicted": round(mean_p, 4),
        "mean_outcome": round(mean_o, 4),
    }


def decision_calibration_curve(window_days: int = 90, buckets: int = 5) -> list[dict]:
    """Group resolved decisions into probability buckets and return actual
    outcome rate per bucket. Default 5 buckets: [0-20], [20-40], [40-60],
    [60-80], [80-100] percent.

    Returns: list of {bucket_low, bucket_high, n, mean_predicted, actual_rate}
    sorted by bucket ascending. Empty list if no resolved decisions in the
    window.
    """
    from datetime import datetime, timedelta, UTC
    cutoff = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
    width = 1.0 / buckets
    out: list[dict] = []
    with _conn() as c:
        for i in range(buckets):
            lo = i * width
            hi = (i + 1) * width
            # Include upper bound only on the last bucket to avoid double-counting.
            upper_op = "<=" if i == buckets - 1 else "<"
            row = c.execute(
                f"""
                SELECT COUNT(*) AS n,
                       COALESCE(AVG(predicted_p), 0.0) AS mean_p,
                       COALESCE(AVG(outcome), 0.0) AS actual_rate
                FROM decisions
                WHERE outcome IS NOT NULL
                  AND resolved_at IS NOT NULL
                  AND resolved_at >= ?
                  AND predicted_p >= ?
                  AND predicted_p {upper_op} ?
                """,
                (cutoff, lo, hi),
            ).fetchone()
            out.append({
                "bucket_low": lo,
                "bucket_high": hi,
                "n": int(row["n"] or 0),
                "mean_predicted": float(row["mean_p"] or 0.0),
                "actual_rate": float(row["actual_rate"] or 0.0),
            })
    return out


# ---------- 5B: messages FTS + fact helpers ----------

def _migrate_messages_fts(conn: sqlite3.Connection) -> None:
    """5B: FTS5 virtual table over the messages table (final sent text).

    Idempotent — returns immediately if messages_fts already exists.
    Per MEMORY.md schema-migration-ordering rule: indexes and triggers for
    ALTER-added columns live inside migration fns, never in _SCHEMA.
    """
    existing_tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'shadow')"
    ).fetchall()}
    if "messages_fts" in existing_tables:
        return
    conn.execute("""
        CREATE VIRTUAL TABLE messages_fts USING fts5(
            content,
            content='messages',
            content_rowid='id',
            tokenize='porter unicode61'
        )
    """)
    # Backfill existing messages.
    conn.execute(
        "INSERT INTO messages_fts(rowid, content) "
        "SELECT id, content FROM messages"
    )
    # Keep mirror in sync.
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS messages_fts_ai
        AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS messages_fts_ad
        AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content)
            VALUES ('delete', old.id, old.content);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS messages_fts_au
        AFTER UPDATE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content)
            VALUES ('delete', old.id, old.content);
            INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
        END
    """)
    conn.commit()


def messages_fts_search(
    query: str,
    limit: int = 10,
    since_iso: str | None = None,
    role: str | None = None,
) -> list[dict[str, Any]]:
    """BM25 FTS5 search over the messages table.

    Returns up to ``limit`` rows ordered by relevance (bm25 ascending, i.e. most
    relevant first) then ts descending. Filters by ``since_iso`` (messages.ts >=)
    and ``role`` when supplied. Returns [] on FTS5 query syntax errors.
    """
    sql = (
        "SELECT m.id, m.role, m.content, m.ts "
        "FROM messages_fts f JOIN messages m ON m.id = f.rowid "
        "WHERE messages_fts MATCH ? AND (m.source IS NULL OR m.source NOT LIKE 'ephemeral:%')"
    )
    params: list[Any] = [query]
    if since_iso:
        sql += " AND m.ts >= ?"
        params.append(since_iso)
    if role:
        sql += " AND m.role = ?"
        params.append(role)
    sql += " ORDER BY bm25(messages_fts), m.ts DESC LIMIT ?"
    params.append(int(limit))
    try:
        with _conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        logger.debug("messages_fts_search: FTS5 query error for %r", query)
        return []


def _migrate_graph_outbox(conn: sqlite3.Connection) -> None:
    """5D: durable Graphiti outbox table.

    All facts write a pending row here in the same transaction as the fact
    INSERT. The scheduler's process_outbox worker drains the queue by calling
    Graphiti's add_episode, marking rows 'sent' on success and 'failed' after 5
    attempts. Idempotent — returns immediately if table already exists.
    Per MEMORY.md schema-migration-ordering rule: indexes live inside migration fn.
    """
    existing = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='graph_outbox'"
    ).fetchall()}
    if "graph_outbox" in existing:
        return
    conn.execute("""
        CREATE TABLE graph_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_table TEXT NOT NULL,
            source_id INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','sent','failed','skipped')),
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at INTEGER NOT NULL,
            processed_at INTEGER
        )
    """)
    conn.execute(
        "CREATE INDEX idx_graph_outbox_status_created "
        "ON graph_outbox(status, created_at)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX idx_graph_outbox_source "
        "ON graph_outbox(source_table, source_id)"
    )


def _migrate_oauth_tokens_to_hash(conn: sqlite3.Connection) -> None:
    """Sprint 7F: copy plaintext oauth_tokens rows to sha256-hashed surface.

    Reads any existing rows from the oauth_tokens table and inserts hashed
    representations into oauth_token_hashes. The oauth_tokens table is NOT
    dropped — the full OAuth 2.1 dance (oauth_token_mint, _oauth2_token_validate)
    continues to use it. This migration is a one-time back-fill that seeds
    oauth_token_hashes from existing access tokens so callers that validate
    via hash can still find pre-existing tokens.

    Safe no-op when oauth_tokens is empty or doesn't exist.
    """
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='oauth_tokens'"
    )
    if not cur.fetchone():
        return  # nothing to migrate (fresh DB)
    rows = conn.execute(
        "SELECT token, client_id, created_at, expires_at, scope FROM oauth_tokens"
    ).fetchall()
    for row in rows:
        token = row[0]
        owner = str(row[1])  # use client_id as owner
        created_at = row[2]
        expires_at = row[3]
        scopes = row[4]
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        conn.execute(
            "INSERT OR IGNORE INTO oauth_token_hashes"
            "(token_hash, owner, created_at, expires_at, scopes) "
            "VALUES (?, ?, ?, ?, ?)",
            (token_hash, owner, created_at, expires_at, scopes),
        )


def _migrate_media_events(conn: sqlite3.Connection) -> None:
    """Create the durable media_events history table if it doesn't exist.

    Records every outbound photo/voice/document after confirmed delivery.
    Indexes live here per the migration-ordering rule.
    """
    existing = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    if "media_events" in existing:
        return
    conn.execute("""
        CREATE TABLE media_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            source_turn_message_id INTEGER,
            telegram_message_id INTEGER,
            caption TEXT,
            content_hash TEXT,
            retention_policy TEXT NOT NULL DEFAULT 'standard',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX idx_media_events_kind_created "
        "ON media_events(kind, created_at)"
    )
    conn.commit()


def _migrate_drop_persona_drift_probes(conn: sqlite3.Connection) -> None:
    """Drop the persona_drift_probes table from deployed DBs.

    Sprint 7C's drift_canary (drift_canary_answers) owns this signal now.
    The table is removed from _SCHEMA so fresh DBs never create it; this
    migration drops it from existing deployments.
    """
    conn.execute("DROP TABLE IF EXISTS persona_drift_probes")


def _migrate_drop_episode_summaries_and_fact_relations(conn: sqlite3.Connection) -> None:
    """Drop episode_summaries and fact_relations from deployed DBs.

    Both tables are removed from _SCHEMA so fresh DBs never create them; this
    migration drops them from existing deployments. No production readers exist.
    """
    conn.execute("DROP TABLE IF EXISTS episode_summaries")
    conn.execute("DROP TABLE IF EXISTS fact_relations")


def media_events_insert(
    kind: str,
    *,
    source_turn_message_id: int | None = None,
    telegram_message_id: int | None = None,
    caption: str | None = None,
    content_hash: str | None = None,
    retention_policy: str = "standard",
) -> int:
    """Record a delivered media event. Returns new row id."""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO media_events "
            "(kind, source_turn_message_id, telegram_message_id, caption, "
            " content_hash, retention_policy, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                kind,
                source_turn_message_id,
                telegram_message_id,
                caption,
                content_hash,
                retention_policy,
                _now(),
            ),
        )
        return int(cur.lastrowid or 0)


def media_events_recent(limit: int = 20) -> list[dict[str, Any]]:
    """Return the most recent media_events rows, newest first."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM media_events ORDER BY created_at DESC, id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def media_events_counts() -> dict[str, int]:
    """Return {kind: count} totals from media_events."""
    with _conn() as c:
        rows = c.execute(
            "SELECT kind, COUNT(*) AS n FROM media_events GROUP BY kind"
        ).fetchall()
    return {r["kind"]: r["n"] for r in rows}


def graph_outbox_insert(source_table: str, source_id: int, payload_json: str,
                        conn=None) -> int | None:
    """Insert pending outbox row. Returns row id, or None on unique conflict (dedup).

    If conn is provided, uses it (caller's transaction). Otherwise opens fresh _conn().
    """
    sql = ("INSERT OR IGNORE INTO graph_outbox "
           "(source_table, source_id, payload_json, created_at) "
           "VALUES (?, ?, ?, strftime('%s','now'))")
    if conn is not None:
        cur = conn.execute(sql, (source_table, int(source_id), payload_json))
        return cur.lastrowid if cur.rowcount else None
    with _conn() as c:
        cur = c.execute(sql, (source_table, int(source_id), payload_json))
        return cur.lastrowid if cur.rowcount else None


def graph_outbox_pending(limit: int = 50) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM graph_outbox WHERE status='pending' "
            "ORDER BY created_at ASC, id ASC LIMIT ?", (int(limit),)
        ).fetchall()
    return [dict(r) for r in rows]


def graph_outbox_mark_sent(row_id: int, processed_at_epoch: int | None = None) -> None:
    with _conn() as c:
        if processed_at_epoch is not None:
            c.execute(
                "UPDATE graph_outbox SET status='sent', processed_at=? WHERE id=?",
                (int(processed_at_epoch), int(row_id))
            )
        else:
            c.execute(
                "UPDATE graph_outbox SET status='sent', "
                "processed_at=CAST(strftime('%s','now') AS INTEGER) WHERE id=?",
                (int(row_id),)
            )


def graph_outbox_mark_failed(row_id: int, error: str) -> None:
    """Increment attempts; flip to status='failed' if attempts+1 >= 5.

    Infrastructure errors (missing OPENROUTER_API_KEY, GRAPHITI_ENABLED=false)
    are treated as transient: rows stay 'pending' with backoff so the outbox
    drains automatically once the env is restored, instead of piling up as
    permanently 'failed'. Permanent errors (bad payload JSON, etc.) still flip
    at the 5-attempt threshold.
    """
    import os as _os
    _TRANSIENT_PREFIXES = (
        "OPENROUTER_API_KEY",
        "GRAPHITI_ENABLED",
    )
    is_transient = any(p in error for p in _TRANSIENT_PREFIXES)
    if is_transient:
        with _conn() as c:
            c.execute(
                "UPDATE graph_outbox SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                (str(error)[:500], int(row_id))
            )
        return
    with _conn() as c:
        c.execute(
            "UPDATE graph_outbox SET "
            "attempts = attempts + 1, "
            "last_error = ?, "
            "status = CASE WHEN attempts + 1 >= 5 THEN 'failed' ELSE status END "
            "WHERE id = ?",
            (str(error)[:500], int(row_id))
        )


def graph_outbox_failed_stats() -> dict:
    """Return failed-row count and the most recent last_error string.

    Used by health.py and cockpit.py to surface actionable error detail.
    """
    with _conn() as c:
        count_row = c.execute(
            "SELECT COUNT(*) AS n FROM graph_outbox WHERE status = 'failed'"
        ).fetchone()
        error_row = c.execute(
            "SELECT last_error FROM graph_outbox WHERE status = 'failed' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    count = (count_row["n"] or 0) if count_row else 0
    last_error = error_row["last_error"] if error_row else None
    return {"count": count, "last_error": last_error}


def graph_outbox_stats() -> dict:
    """Return counts by status, zero-filling missing statuses.

    Returns ``{pending, sent, failed, skipped, drained}`` via COUNT(*) GROUP BY.
    Drained rows are manual-drain (never retried); excluded from failed-count math.
    """
    stats = {"pending": 0, "sent": 0, "failed": 0, "skipped": 0, "drained": 0}
    with _conn() as c:
        for row in c.execute(
            "SELECT status, COUNT(*) AS n FROM graph_outbox GROUP BY status"
        ).fetchall():
            stats[row["status"]] = row["n"]
    return stats


def graph_outbox_mark_drained(ids: Iterable[int]) -> int:
    """Manually drain graph_outbox rows by writing status='drained'.

    Drained rows are excluded from failed-count math and not retried.
    Returns the number of rows updated.
    """
    id_list = [int(i) for i in ids]
    if not id_list:
        return 0
    placeholders = ",".join("?" * len(id_list))
    with _conn() as c:
        cur = c.execute(
            f"UPDATE graph_outbox SET status='drained' WHERE id IN ({placeholders})",
            id_list,
        )
    return int(cur.rowcount or 0)


# ---------- media_outbox ----------

def media_outbox_insert(
    kind: str,
    idempotency_key: str,
    payload: dict,
    *,
    conn=None,
) -> int | None:
    """INSERT OR IGNORE into media_outbox. Returns row id if inserted, None on dedup."""
    import json as _json
    sql = (
        "INSERT OR IGNORE INTO media_outbox "
        "(kind, idempotency_key, payload_json, created_at) "
        "VALUES (?, ?, ?, ?)"
    )
    args = (kind, idempotency_key, _json.dumps(payload, ensure_ascii=False), _now())
    if conn is not None:
        cur = conn.execute(sql, args)
        return cur.lastrowid if cur.rowcount else None
    with _conn() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid if cur.rowcount else None


def media_outbox_pending(kind: str | None = None, limit: int = 50) -> list[dict]:
    """Return pending rows ordered oldest-first. Optional kind filter."""
    sql = "SELECT * FROM media_outbox WHERE status='pending'"
    args: list = []
    if kind is not None:
        sql += " AND kind=?"
        args.append(kind)
    sql += " ORDER BY created_at ASC, id ASC LIMIT ?"
    args.append(int(limit))
    with _conn() as c:
        rows = c.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def media_outbox_mark_sent(row_id: int, telegram_message_id: int | None = None) -> None:
    now = _now()
    with _conn() as c:
        c.execute(
            "UPDATE media_outbox SET status='sent', processed_at=?, "
            "telegram_message_id=? WHERE id=?",
            (now, telegram_message_id, int(row_id)),
        )


def media_outbox_mark_failed(
    row_id: int, error: str, *, max_attempts: int | None = None
) -> None:
    """Mark failed. Increments attempts; flips to 'failed' when budget exhausted.

    max_attempts: explicit retry budget. None → kind-based default (5 for photo,
    1 for all others — matches pre-9A behavior).
    """
    now = _now()
    if max_attempts is not None:
        limit = int(max_attempts)
        with _conn() as c:
            c.execute(
                "UPDATE media_outbox SET "
                "attempts = attempts + 1, "
                "last_error = ?, "
                "processed_at = CASE "
                "  WHEN attempts + 1 < ? THEN processed_at "
                "  ELSE ? "
                "END, "
                "status = CASE "
                "  WHEN attempts + 1 < ? THEN status "
                "  ELSE 'failed' "
                "END "
                "WHERE id = ?",
                (str(error)[:500], limit, now, limit, int(row_id)),
            )
    else:
        with _conn() as c:
            c.execute(
                "UPDATE media_outbox SET "
                "attempts = attempts + 1, "
                "last_error = ?, "
                "processed_at = CASE "
                "  WHEN kind = 'photo' AND attempts + 1 < 5 THEN processed_at "
                "  ELSE ? "
                "END, "
                "status = CASE "
                "  WHEN kind = 'photo' AND attempts + 1 < 5 THEN status "
                "  ELSE 'failed' "
                "END "
                "WHERE id = ?",
                (str(error)[:500], now, int(row_id)),
            )


def media_outbox_mark_aborted(row_id: int, reason: str) -> None:
    now = _now()
    with _conn() as c:
        c.execute(
            "UPDATE media_outbox SET status='aborted', last_error=?, processed_at=? WHERE id=?",
            (str(reason)[:500], now, int(row_id)),
        )


def media_outbox_stats() -> dict:
    """Return {status: count} dict. Zero-fills all known statuses."""
    stats = {"pending": 0, "sent": 0, "failed": 0, "aborted": 0}
    with _conn() as c:
        for row in c.execute(
            "SELECT status, COUNT(*) AS n FROM media_outbox GROUP BY status"
        ).fetchall():
            stats[row["status"]] = row["n"]
    return stats


def proactive_events_stale_reserved(cutoff_iso: str) -> list[dict]:
    """Return proactive_events rows with status='reserved' created before cutoff_iso."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM proactive_events WHERE status='reserved' AND sent_at < ?",
            (cutoff_iso,),
        ).fetchall()
    return [dict(r) for r in rows]


def fact_by_id(fact_id: int) -> dict[str, Any] | None:
    """Fetch a single fact row by id. Returns None if not found."""
    with _conn() as c:
        row = c.execute("SELECT * FROM facts WHERE id = ?", (int(fact_id),)).fetchone()
    return dict(row) if row else None


def facts_text_search(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Substring search over active facts (subject / predicate / object).

    Returns up to ``limit`` rows ordered by id DESC (most recently inserted first).
    Only returns facts with valid_to IS NULL (active facts).
    """
    q = f"%{(query or '').strip()}%"
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM facts "
            "WHERE valid_to IS NULL "
            "AND (subject LIKE ? OR predicate LIKE ? OR object LIKE ?) "
            "ORDER BY id DESC LIMIT ?",
            (q, q, q, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


# ============================================================================
# Sprint A — helper functions (peer_insights, diary, work_packets, scores)
# ============================================================================

def peer_insight_insert(observation: str, *, surface_score: float = 0.5,
                        source: str | None = None) -> int:
    """Record a non-explicit observation from the dialectic extractor."""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO peer_insights (observation, surface_score, source, created_at) "
            "VALUES (?, ?, ?, strftime('%s','now'))",
            (observation, float(surface_score), source),
        )
    return int(cur.lastrowid or 0)


def peer_insights_unsurfaced(limit: int = 3) -> list[dict[str, Any]]:
    """Top-N unsurfaced peer insights ordered by surface_score DESC."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM peer_insights WHERE surfaced_at IS NULL "
            "ORDER BY surface_score DESC, created_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def peer_insight_mark_surfaced(insight_id: int) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE peer_insights SET surfaced_at = strftime('%s','now') WHERE id = ?",
            (int(insight_id),),
        )


def diary_entry_upsert(entry_date: str, body: str, *, sentiment: str | None = None,
                       session_ids_json: str | None = None) -> int:
    """One diary row per date (UNIQUE on entry_date). Upsert overwrites body."""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO diary_entries (entry_date, body, sentiment, session_ids_json, created_at) "
            "VALUES (?, ?, ?, ?, strftime('%s','now')) "
            "ON CONFLICT(entry_date) DO UPDATE SET "
            "body=excluded.body, sentiment=excluded.sentiment, session_ids_json=excluded.session_ids_json",
            (entry_date, body, sentiment, session_ids_json),
        )
    return int(cur.lastrowid or 0)


def diary_entries_recent(limit: int = 5) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM diary_entries ORDER BY entry_date DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def diary_entry_get(entry_date: str) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM diary_entries WHERE entry_date = ?", (entry_date,),
        ).fetchone()
    return dict(row) if row else None


def work_packet_create(user_turn_id: str, *, summary: str | None = None) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO work_packets (user_turn_id, status, summary, created_at) "
            "VALUES (?, 'planning', ?, strftime('%s','now'))",
            (user_turn_id, summary),
        )
    return int(cur.lastrowid or 0)


def work_packet_update_status(packet_id: int, status: str, *, finished: bool = False) -> None:
    finished_clause = ", finished_at = strftime('%s','now')" if finished else ""
    with _conn() as c:
        c.execute(
            f"UPDATE work_packets SET status = ?{finished_clause} WHERE id = ?",
            (status, int(packet_id)),
        )


def work_packet_step_insert(packet_id: int, step_index: int, tool_name: str,
                            *, input_json: str | None = None) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO work_packet_steps "
            "(packet_id, step_index, tool_name, status, input_json, created_at) "
            "VALUES (?, ?, ?, 'pending', ?, strftime('%s','now'))",
            (int(packet_id), int(step_index), tool_name, input_json),
        )
    return int(cur.lastrowid or 0)


def work_packet_step_update(step_id: int, *, status: str | None = None,
                            output_json: str | None = None, error: str | None = None,
                            finished: bool = False) -> None:
    parts: list[str] = []
    args: list[Any] = []
    if status is not None:
        parts.append("status = ?")
        args.append(status)
    if output_json is not None:
        parts.append("output_json = ?")
        args.append(output_json)
    if error is not None:
        parts.append("error = ?")
        args.append(error)
    if finished:
        parts.append("finished_at = strftime('%s','now')")
    if not parts:
        return
    args.append(int(step_id))
    with _conn() as c:
        c.execute(f"UPDATE work_packet_steps SET {', '.join(parts)} WHERE id = ?", args)


def work_packet_steps(packet_id: int) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM work_packet_steps WHERE packet_id = ? ORDER BY step_index",
            (int(packet_id),),
        ).fetchall()
    return [dict(r) for r in rows]


def proactive_source_score_upsert(source: str, *, ema: float | None = None,
                                  thumbs_up: int = 0, thumbs_down: int = 0,
                                  ping: bool = False) -> None:
    """Update per-source score row. EMA overwrites if provided; counters increment."""
    with _conn() as c:
        c.execute(
            "INSERT INTO proactive_source_scores "
            "(source, ema, n_pings, n_thumbs_up, n_thumbs_down, last_update) "
            "VALUES (?, ?, ?, ?, ?, strftime('%s','now')) "
            "ON CONFLICT(source) DO UPDATE SET "
            " ema = COALESCE(?, proactive_source_scores.ema), "
            " n_pings = proactive_source_scores.n_pings + ?, "
            " n_thumbs_up = proactive_source_scores.n_thumbs_up + ?, "
            " n_thumbs_down = proactive_source_scores.n_thumbs_down + ?, "
            " last_update = strftime('%s','now')",
            (source, ema if ema is not None else 0.5,
             1 if ping else 0, int(thumbs_up), int(thumbs_down),
             ema, 1 if ping else 0, int(thumbs_up), int(thumbs_down)),
        )


def proactive_source_scores_all() -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM proactive_source_scores ORDER BY ema DESC, last_update DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def _fact_active(fact_id: int) -> bool:
    """True iff the fact is active (valid_to IS NULL AND status='active')."""
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM facts WHERE id = ? AND valid_to IS NULL AND status = 'active'",
            (int(fact_id),),
        ).fetchone()
    return row is not None


def status_counts() -> dict[str, dict[str, int]]:
    """Bucketed counts by status for cockpit-facing tables.

    Returns ``{table_name: {status: count, ...}, ...}``.
    Used by cockpit `/status` and observability paths.
    """
    out: dict[str, dict[str, int]] = {}
    queries = (
        ("graph_outbox", "SELECT status, COUNT(*) AS n FROM graph_outbox GROUP BY status"),
        ("media_outbox", "SELECT status, COUNT(*) AS n FROM media_outbox GROUP BY status"),
        ("facts", "SELECT status, COUNT(*) AS n FROM facts GROUP BY status"),
        ("reminders", "SELECT status, COUNT(*) AS n FROM reminders GROUP BY status"),
        ("proactive_events", "SELECT status, COUNT(*) AS n FROM proactive_events GROUP BY status"),
        ("work_packets", "SELECT status, COUNT(*) AS n FROM work_packets GROUP BY status"),
    )
    with _conn() as c:
        for table, sql in queries:
            bucket: dict[str, int] = {}
            try:
                for row in c.execute(sql):
                    bucket[str(row["status"] or "_null")] = int(row["n"])
            except sqlite3.OperationalError:
                continue
            out[table] = bucket
    return out


def prune_tool_calls(older_than_days: int = 30) -> int:
    from datetime import timedelta
    cutoff_iso = (datetime.now(UTC) - timedelta(days=int(older_than_days))).isoformat()
    with _conn() as c:
        cur = c.execute("DELETE FROM tool_calls WHERE started_at < ?", (cutoff_iso,))
    return int(cur.rowcount or 0)


def prune_graph_outbox_sent(older_than_days: int = 14) -> int:
    cutoff = _now_epoch() - int(older_than_days) * 86400
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM graph_outbox WHERE status IN ('sent','drained','skipped') AND COALESCE(processed_at, created_at) < ?",
            (cutoff,),
        )
    return int(cur.rowcount or 0)


def prune_media_outbox_terminal(older_than_days: int = 14) -> int:
    from datetime import timedelta
    cutoff_iso = (datetime.now(UTC) - timedelta(days=int(older_than_days))).isoformat()
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM media_outbox WHERE status IN ('sent','failed','aborted') AND created_at < ?",
            (cutoff_iso,),
        )
    return int(cur.rowcount or 0)


def prune_proactive_events(older_than_days: int = 90) -> int:
    from datetime import timedelta
    cutoff_iso = (datetime.now(UTC) - timedelta(days=int(older_than_days))).isoformat()
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM proactive_events WHERE sent_at < ?",
            (cutoff_iso,),
        )
    return int(cur.rowcount or 0)


def _now_epoch() -> int:
    return int(datetime.now(UTC).timestamp())


# ---------- belief_journal ----------

def belief_journal_insert(*, statement: str, claim_type: str, resurface_days: int = 90) -> int:
    """Insert into belief_journal table.

    Returns the new row id (> 0) or 0 on a no-op (empty statement).
    claim_type must be 'factual' or 'identity'.
    """
    from datetime import timedelta
    if claim_type not in ("factual", "identity"):
        raise ValueError(f"invalid claim_type: {claim_type}")
    text = (statement or "").strip()[:500]
    if not text:
        return 0
    resurface = (datetime.now(UTC) + timedelta(days=resurface_days)).isoformat()
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO belief_journal (stated_at, statement, claim_type, resurface_at, resolved_bool) "
            "VALUES (?, ?, ?, ?, 0)",
            (_now(), text, claim_type, resurface),
        )
        return int(cur.lastrowid or 0)


def belief_journal_due(window_days: int = 0) -> list[dict]:
    """Return matured (resurface_at <= now + window_days), unresolved beliefs.

    window_days=0 = strictly due; >0 = approaching.
    """
    from datetime import timedelta
    cutoff = (datetime.now(UTC) + timedelta(days=window_days)).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT id, stated_at, statement, claim_type, resurface_at "
            "FROM belief_journal "
            "WHERE resolved_bool = 0 AND resurface_at <= ? "
            "ORDER BY resurface_at ASC LIMIT 10",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def belief_journal_resolve(belief_id: int, note: str | None = None) -> None:
    """Mark a belief_journal row as resolved, optionally with a note."""
    with _conn() as c:
        c.execute(
            "UPDATE belief_journal SET resolved_bool = 1, resolution_note = ? WHERE id = ?",
            ((note or "")[:300], belief_id),
        )
