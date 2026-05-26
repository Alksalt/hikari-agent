"""Pruner test suite.

Section A — Sprint 3 pruners:
  - audit_log (intentionally absent — hash-chain integrity)
  - oauth_audit_log
  - calendar_notifications

Section B — Sprint A pruners (new retention-management functions):
  - prune_tool_calls(older_than_days=30)
  - prune_graph_outbox_sent(older_than_days=14)
  - prune_media_outbox_terminal(older_than_days=14)
  - prune_proactive_events(older_than_days=90)
"""
from __future__ import annotations

import importlib
import time
from pathlib import Path

import pytest

from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    # Bootstrap schema.
    db.upsert_core_block("_boot", "_boot")
    yield
    db._reset_schema_sentinel()


# ---------- helpers ----------

def _trigger_schema():
    """Bootstrap schema by writing one row."""
    db.upsert_core_block("_ping", "_ping")


def _insert_audit_rows(n_old: int, n_fresh: int, days_old: int = 400) -> None:
    _trigger_schema()
    with db._conn() as c:
        for _ in range(n_old):
            c.execute(
                "INSERT INTO audit_log (ts, tool, args_json_redacted, hash_self) "
                "VALUES (datetime('now', ? || ' days'), ?, ?, ?)",
                (f"-{days_old}", "t", "{}", "x"),
            )
        for _ in range(n_fresh):
            c.execute(
                "INSERT INTO audit_log (ts, tool, args_json_redacted, hash_self) "
                "VALUES (datetime('now'), ?, ?, ?)",
                ("t", "{}", "y"),
            )


def _insert_oauth_audit_rows(n_old: int, n_fresh: int, days_old: int = 400) -> None:
    _trigger_schema()
    with db._conn() as c:
        for _ in range(n_old):
            c.execute(
                "INSERT INTO oauth_audit_log (ts, event_type) "
                "VALUES (datetime('now', ? || ' days'), ?)",
                (f"-{days_old}", "login"),
            )
        for _ in range(n_fresh):
            c.execute(
                "INSERT INTO oauth_audit_log (ts, event_type) "
                "VALUES (datetime('now'), ?)",
                ("login",),
            )


# ---------- audit_log pruner (intentionally absent — hash-chain integrity) ----------

def test_prune_audit_log_is_intentionally_absent():
    """audit_log must NOT have a prune function.

    The table is a SHA-chained forensic ledger: each row's hash_prev references
    the previous row's hash_self (see db.audit_append). Deleting the oldest row
    would invalidate every subsequent hash_prev reference, breaking the entire
    chain. Pruning audit_log is therefore permanently off the table.
    oauth_audit_log is explicitly NOT chained and does have a pruner.
    """
    assert not hasattr(db, "prune_audit_log_older_than_days")


# ---------- oauth_audit_log pruner ----------

def test_prune_oauth_audit_log_older_than_days():
    _insert_oauth_audit_rows(n_old=3, n_fresh=3, days_old=400)
    deleted = db.prune_oauth_audit_log_older_than_days(365)
    assert deleted == 3
    with db._conn() as c:
        remaining = c.execute("SELECT COUNT(*) AS n FROM oauth_audit_log").fetchone()["n"]
    assert remaining == 3


def test_prune_oauth_audit_log_empty():
    _trigger_schema()
    assert db.prune_oauth_audit_log_older_than_days(365) == 0


# ---------- calendar_notifications pruner + helpers ----------

def test_calendar_notification_set_and_exists():
    _trigger_schema()
    assert not db.calendar_notification_exists("abc123")
    db.calendar_notification_set("abc123")
    assert db.calendar_notification_exists("abc123")
    # idempotent
    db.calendar_notification_set("abc123")
    assert db.calendar_notification_exists("abc123")
    assert not db.calendar_notification_exists("unknown_sig")


def test_prune_calendar_notifications_older_than_days():
    _trigger_schema()
    db.calendar_notification_set("fresh_sig")
    db.calendar_notification_set("old_sig")
    # Backdate old_sig by 100 days.
    with db._conn() as c:
        c.execute(
            "UPDATE calendar_notifications SET notified_at = datetime('now', '-100 days') "
            "WHERE signature = 'old_sig'"
        )
    deleted = db.prune_calendar_notifications_older_than_days(90)
    assert deleted == 1
    assert db.calendar_notification_exists("fresh_sig")
    assert not db.calendar_notification_exists("old_sig")


def test_prune_calendar_notifications_empty():
    _trigger_schema()
    assert db.prune_calendar_notifications_older_than_days(90) == 0


# ---------- migration backfill ----------

def test_calendar_notifications_migration_backfills_kv_keys():
    _trigger_schema()
    # Seed two kv keys in runtime_state that the old code used.
    db.runtime_set("calendar_notified_sig_a", "2026-01-01T00:00:00")
    db.runtime_set("calendar_notified_sig_b", "2026-01-02T00:00:00")
    # Force re-run of the migration against the live connection.
    with db._conn() as c:
        # Drop the table so the migration recreates and backfills it.
        c.execute("DROP TABLE IF EXISTS calendar_notifications")
        from storage.db import _migrate_calendar_notifications
        _migrate_calendar_notifications(c)
    assert db.calendar_notification_exists("sig_a")
    assert db.calendar_notification_exists("sig_b")
    # kv keys still present (not deleted by migration).
    assert db.runtime_get("calendar_notified_sig_a") is not None


# ---------- scheduler job covers all 3 active pruners ----------

def test_monthly_prune_job_calls_all_pruners(monkeypatch):
    """All 3 pruner functions are called by the monthly job.

    audit_log is intentionally excluded — see test_prune_audit_log_is_intentionally_absent.
    persona_drift_probes pruner removed in Sprint 10C (table dropped via migration).
    """
    _trigger_schema()
    calls: dict[str, int] = {}

    def _track(name):
        def _fn(days):
            calls[name] = days
            return 0
        return _fn

    monkeypatch.setattr(db, "prune_messages_older_than_days", _track("messages"))
    monkeypatch.setattr(db, "prune_oauth_audit_log_older_than_days", _track("oauth_audit"))
    monkeypatch.setattr(db, "prune_calendar_notifications_older_than_days", _track("calendar"))

    import asyncio

    from agents import config as cfg
    cfg.reload()

    # Extract the inner coroutine by building the scheduler and finding the job.
    from agents.scheduler import build_scheduler

    async def _fake_send(s):
        return None

    sched = build_scheduler(_fake_send)
    # Find _monthly_prune_job by id.
    job = next(j for j in sched.get_jobs() if j.id == "monthly_prune")
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(job.func())
    finally:
        loop.close()

    assert "messages" in calls
    assert "oauth_audit" in calls
    assert "calendar" in calls


# ===========================================================================
# Section B — Sprint A pruners
# ===========================================================================

# ---------------------------------------------------------------------------
# prune_tool_calls
# ---------------------------------------------------------------------------

def _insert_tool_call(days_ago: int, idx: int) -> None:
    """Insert a tool_calls row with started_at backdated by days_ago."""
    with db._conn() as c:
        c.execute(
            "INSERT INTO tool_calls (tool_id, started_at, duration_ms, success, output_size) "
            "VALUES (?, datetime('now', ? || ' days'), 10, 1, 0)",
            (f"tc-{idx}", f"-{days_ago}"),
        )


def test_prune_tool_calls_deletes_old_keeps_recent():
    """3 rows older than 30d deleted; 2 recent rows retained."""
    _insert_tool_call(31, 1)
    _insert_tool_call(60, 2)
    _insert_tool_call(90, 3)
    _insert_tool_call(1, 4)
    _insert_tool_call(5, 5)

    deleted = db.prune_tool_calls(older_than_days=30)

    assert deleted == 3
    with db._conn() as c:
        remaining = c.execute("SELECT COUNT(*) AS n FROM tool_calls").fetchone()["n"]
    assert remaining == 2


def test_prune_tool_calls_empty_table():
    assert db.prune_tool_calls(older_than_days=30) == 0


def test_prune_tool_calls_all_recent_nothing_deleted():
    _insert_tool_call(1, 10)
    _insert_tool_call(5, 11)
    _insert_tool_call(10, 12)

    assert db.prune_tool_calls(older_than_days=30) == 0
    with db._conn() as c:
        remaining = c.execute("SELECT COUNT(*) AS n FROM tool_calls").fetchone()["n"]
    assert remaining == 3


def test_prune_tool_calls_boundary_29d_retained():
    """A row inserted 29 days ago is safely within the retention window and kept.

    Note: ``datetime('now', '-30 days')`` at insert time will be slightly older
    than the cutoff computed at prune time, so rows inserted with -30d reliably
    get deleted too.  The meaningful boundary to test is retention (29d) vs
    clearly-over (31d).
    """
    _insert_tool_call(29, 20)   # within retention — kept
    _insert_tool_call(31, 21)   # clearly over — deleted

    deleted = db.prune_tool_calls(older_than_days=30)
    assert deleted == 1
    with db._conn() as c:
        remaining = c.execute("SELECT COUNT(*) AS n FROM tool_calls").fetchone()["n"]
    assert remaining == 1


# ---------------------------------------------------------------------------
# prune_graph_outbox_sent
# ---------------------------------------------------------------------------

def _insert_graph_outbox(status: str, days_ago_processed: int, sid: int) -> None:
    epoch_offset = int(time.time()) - days_ago_processed * 86400
    with db._conn() as c:
        c.execute(
            "INSERT INTO graph_outbox "
            "(source_table, source_id, payload_json, status, created_at, processed_at) "
            "VALUES ('facts', ?, '{}', ?, ?, ?)",
            (sid, status, epoch_offset - 1, epoch_offset),
        )


def test_prune_graph_outbox_sent_deletes_old_terminal():
    """Old sent/drained/skipped rows deleted; recent sent and old pending retained."""
    _insert_graph_outbox("sent", 20, 101)
    _insert_graph_outbox("sent", 20, 102)
    _insert_graph_outbox("drained", 20, 103)
    _insert_graph_outbox("skipped", 20, 104)
    _insert_graph_outbox("sent", 5, 105)      # recent — kept
    _insert_graph_outbox("pending", 20, 106)  # not terminal — kept

    deleted = db.prune_graph_outbox_sent(older_than_days=14)

    assert deleted == 4
    with db._conn() as c:
        remaining = c.execute("SELECT COUNT(*) AS n FROM graph_outbox").fetchone()["n"]
    assert remaining == 2


def test_prune_graph_outbox_empty():
    assert db.prune_graph_outbox_sent(older_than_days=14) == 0


def test_prune_graph_outbox_pending_and_failed_never_deleted():
    """pending and failed rows are not terminal and must not be pruned."""
    _insert_graph_outbox("pending", 100, 110)
    _insert_graph_outbox("failed", 100, 111)

    assert db.prune_graph_outbox_sent(older_than_days=14) == 0


# ---------------------------------------------------------------------------
# prune_media_outbox_terminal
# ---------------------------------------------------------------------------

def _insert_media_outbox(status: str, days_ago: int, key: str) -> None:
    with db._conn() as c:
        c.execute(
            "INSERT INTO media_outbox "
            "(kind, idempotency_key, payload_json, status, created_at) "
            "VALUES ('text', ?, '{}', ?, datetime('now', ? || ' days'))",
            (key, status, f"-{days_ago}"),
        )


def test_prune_media_outbox_terminal_deletes_old():
    """Old terminal rows (sent/failed/aborted) deleted; recent + pending retained."""
    _insert_media_outbox("sent", 20, "m1")
    _insert_media_outbox("failed", 20, "m2")
    _insert_media_outbox("aborted", 20, "m3")
    _insert_media_outbox("sent", 5, "m4")     # recent — kept
    _insert_media_outbox("pending", 20, "m5") # not terminal — kept

    deleted = db.prune_media_outbox_terminal(older_than_days=14)

    assert deleted == 3
    with db._conn() as c:
        remaining = c.execute("SELECT COUNT(*) AS n FROM media_outbox").fetchone()["n"]
    assert remaining == 2


def test_prune_media_outbox_empty():
    assert db.prune_media_outbox_terminal(older_than_days=14) == 0


def test_prune_media_outbox_pending_never_deleted():
    _insert_media_outbox("pending", 100, "mp1")
    _insert_media_outbox("pending", 50, "mp2")

    assert db.prune_media_outbox_terminal(older_than_days=14) == 0
    with db._conn() as c:
        remaining = c.execute("SELECT COUNT(*) AS n FROM media_outbox").fetchone()["n"]
    assert remaining == 2


# ---------------------------------------------------------------------------
# prune_proactive_events
# ---------------------------------------------------------------------------

def _insert_proactive_event(days_ago: int) -> None:
    with db._conn() as c:
        c.execute(
            "INSERT INTO proactive_events "
            "(sent_at, source, pattern, payload_json) "
            "VALUES (datetime('now', ? || ' days'), 'test', 'p', '{}')",
            (f"-{days_ago}",),
        )


def test_prune_proactive_events_deletes_old():
    """Rows older than threshold deleted; recent rows retained."""
    _insert_proactive_event(100)
    _insert_proactive_event(100)
    _insert_proactive_event(100)
    _insert_proactive_event(30)
    _insert_proactive_event(10)

    deleted = db.prune_proactive_events(older_than_days=90)

    assert deleted == 3
    with db._conn() as c:
        remaining = c.execute("SELECT COUNT(*) AS n FROM proactive_events").fetchone()["n"]
    assert remaining == 2


def test_prune_proactive_events_empty():
    assert db.prune_proactive_events(older_than_days=90) == 0


def test_prune_proactive_events_all_recent_nothing_deleted():
    _insert_proactive_event(10)
    _insert_proactive_event(30)
    _insert_proactive_event(60)

    assert db.prune_proactive_events(older_than_days=90) == 0
    with db._conn() as c:
        remaining = c.execute("SELECT COUNT(*) AS n FROM proactive_events").fetchone()["n"]
    assert remaining == 3


def test_prune_proactive_events_short_threshold():
    """Short threshold (7d) only removes rows clearly over the line."""
    _insert_proactive_event(10)  # deleted
    _insert_proactive_event(5)   # kept
    _insert_proactive_event(1)   # kept

    deleted = db.prune_proactive_events(older_than_days=7)
    assert deleted == 1
    with db._conn() as c:
        remaining = c.execute("SELECT COUNT(*) AS n FROM proactive_events").fetchone()["n"]
    assert remaining == 2
