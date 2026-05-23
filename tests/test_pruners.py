"""Phase A (Sprint 3) — pruner functions for audit_log, oauth_audit_log,
persona_drift_probes, and calendar_notifications; plus scheduler job coverage."""
from __future__ import annotations

import importlib
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
    yield


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


def _insert_drift_probe_rows(n_old: int, n_fresh: int, days_old: int = 200) -> None:
    _trigger_schema()
    with db._conn() as c:
        for _ in range(n_old):
            c.execute(
                "INSERT INTO persona_drift_probes (probe_key, distance, created_at) "
                "VALUES (?, ?, datetime('now', ? || ' days'))",
                ("values", 0.1, f"-{days_old}"),
            )
        for _ in range(n_fresh):
            c.execute(
                "INSERT INTO persona_drift_probes (probe_key, distance, created_at) "
                "VALUES (?, ?, datetime('now'))",
                ("values", 0.1),
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


# ---------- drift_probes pruner ----------

def test_prune_drift_probes_older_than_days():
    _insert_drift_probe_rows(n_old=2, n_fresh=2, days_old=200)
    deleted = db.prune_drift_probes_older_than_days(180)
    assert deleted == 2
    with db._conn() as c:
        remaining = c.execute("SELECT COUNT(*) AS n FROM persona_drift_probes").fetchone()["n"]
    assert remaining == 2


def test_prune_drift_probes_empty():
    _trigger_schema()
    assert db.prune_drift_probes_older_than_days(180) == 0


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


# ---------- scheduler job covers all 4 pruners ----------

def test_monthly_prune_job_calls_all_pruners(monkeypatch):
    """All 4 pruner functions are called by the monthly job.

    audit_log is intentionally excluded — see test_prune_audit_log_is_intentionally_absent.
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
    monkeypatch.setattr(db, "prune_drift_probes_older_than_days", _track("drift"))
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
    assert "drift" in calls
    assert "calendar" in calls
