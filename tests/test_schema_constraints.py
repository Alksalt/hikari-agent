"""Verify CHECK constraints in storage/db.py are actually enforced by SQLite.

Tables with documented CHECK constraints:
  - tasks.status              : pending | in_progress | completed | dropped
  - background_tasks.status   : queued | running | done | failed | cancelled
  - approvals.status          : pending | approved | rejected | timeout
  - media_outbox.status       : pending | sent | failed | aborted
  - graph_outbox.status       : pending | sent | failed | skipped | drained
  - work_packets.status       : planning | running | done | failed | cancelled | waiting
  - work_packet_steps.status  : pending | running | done | waiting | failed | skipped | cancelled

Tables WITHOUT CHECK constraints on status (gap tests, marked xfail):
  - facts.status          (added via ALTER, no CHECK clause)
  - reminders.status      (schema has DEFAULT 'active', no CHECK clause)
  - proactive_events.status (added via ALTER, no CHECK clause)
"""
from __future__ import annotations

import importlib
import sqlite3
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


# ---------------------------------------------------------------------------
# tasks.status  CHECK (status IN ('pending', 'in_progress', 'completed', 'dropped'))
# ---------------------------------------------------------------------------

class TestTasksStatus:
    def test_valid_pending(self):
        tid = db.create_task("test task")
        with db._conn() as c:
            row = c.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["status"] == "pending"

    def test_valid_in_progress(self):
        tid = db.create_task("t2")
        db.update_task(tid, status="in_progress")
        with db._conn() as c:
            row = c.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["status"] == "in_progress"

    def test_valid_completed(self):
        tid = db.create_task("t3")
        db.update_task(tid, status="completed")
        with db._conn() as c:
            row = c.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["status"] == "completed"

    def test_valid_dropped(self):
        tid = db.create_task("t4")
        db.update_task(tid, status="dropped")
        with db._conn() as c:
            row = c.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["status"] == "dropped"

    def test_invalid_status_raises(self):
        with db._conn() as c:
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(
                    "INSERT INTO tasks (subject, status, created_at) VALUES (?, ?, datetime('now'))",
                    ("bad-task", "bogus_status"),
                )


# ---------------------------------------------------------------------------
# background_tasks.status
# CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled'))
# ---------------------------------------------------------------------------

class TestBackgroundTasksStatus:
    def _insert(self, status: str) -> None:
        with db._conn() as c:
            c.execute(
                "INSERT INTO background_tasks "
                "(task_id, kind, chat_id, prompt, status, started_at) "
                "VALUES (?, 'test', 1, 'p', ?, datetime('now'))",
                (f"tid-{status}", status),
            )

    @pytest.mark.parametrize("status", ["queued", "running", "done", "failed", "cancelled"])
    def test_valid_statuses(self, status):
        self._insert(status)  # should not raise

    def test_invalid_status_raises(self):
        with db._conn() as c:
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(
                    "INSERT INTO background_tasks "
                    "(task_id, kind, chat_id, prompt, status, started_at) "
                    "VALUES ('x', 'test', 1, 'p', 'NOPE', datetime('now'))"
                )


# ---------------------------------------------------------------------------
# approvals.status
# CHECK (status IN ('pending', 'approved', 'rejected', 'timeout'))
# ---------------------------------------------------------------------------

class TestApprovalsStatus:
    def _insert(self, status: str) -> None:
        with db._conn() as c:
            c.execute(
                "INSERT INTO approvals "
                "(chat_id, tool_name, tier, summary, args_json, status, created_at) "
                "VALUES (1, 'tool', 1, 'sum', '{}', ?, datetime('now'))",
                (status,),
            )

    @pytest.mark.parametrize("status", ["pending", "approved", "rejected", "timeout"])
    def test_valid_statuses(self, status):
        self._insert(status)  # should not raise

    def test_invalid_status_raises(self):
        with db._conn() as c:
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(
                    "INSERT INTO approvals "
                    "(chat_id, tool_name, tier, summary, args_json, status, created_at) "
                    "VALUES (1, 't', 1, 's', '{}', 'INVALID', datetime('now'))"
                )


# ---------------------------------------------------------------------------
# media_outbox.status
# CHECK (status IN ('pending', 'sent', 'failed', 'aborted'))
# ---------------------------------------------------------------------------

class TestMediaOutboxStatus:
    def _insert(self, status: str, key: str) -> None:
        with db._conn() as c:
            c.execute(
                "INSERT INTO media_outbox "
                "(kind, idempotency_key, payload_json, status, created_at) "
                "VALUES ('text', ?, '{}', ?, datetime('now'))",
                (key, status),
            )

    @pytest.mark.parametrize("status", ["pending", "sent", "failed", "aborted"])
    def test_valid_statuses(self, status):
        self._insert(status, f"key-{status}")

    def test_invalid_status_raises(self):
        with db._conn() as c:
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(
                    "INSERT INTO media_outbox "
                    "(kind, idempotency_key, payload_json, status, created_at) "
                    "VALUES ('text', 'bad-key', '{}', 'WRONG', datetime('now'))"
                )


# ---------------------------------------------------------------------------
# graph_outbox.status
# CHECK (status IN ('pending','sent','failed','skipped','drained'))
# ---------------------------------------------------------------------------

class TestGraphOutboxStatus:
    def _insert(self, status: str, sid: int) -> None:
        with db._conn() as c:
            c.execute(
                "INSERT INTO graph_outbox "
                "(source_table, source_id, payload_json, status, created_at) "
                "VALUES ('facts', ?, '{}', ?, unixepoch())",
                (sid, status),
            )

    @pytest.mark.parametrize("status,sid", [
        ("pending", 1), ("sent", 2), ("failed", 3), ("skipped", 4), ("drained", 5),
    ])
    def test_valid_statuses(self, status, sid):
        self._insert(status, sid)

    def test_invalid_status_raises(self):
        with db._conn() as c:
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(
                    "INSERT INTO graph_outbox "
                    "(source_table, source_id, payload_json, status, created_at) "
                    "VALUES ('facts', 999, '{}', 'BAD', unixepoch())"
                )


# ---------------------------------------------------------------------------
# work_packets.status
# CHECK (status IN ('planning','running','done','failed','cancelled','waiting'))
# ---------------------------------------------------------------------------

class TestWorkPacketsStatus:
    def _insert(self, status: str) -> int:
        with db._conn() as c:
            cur = c.execute(
                "INSERT INTO work_packets (user_turn_id, status, created_at) "
                "VALUES ('turn-1', ?, unixepoch())",
                (status,),
            )
            return cur.lastrowid

    @pytest.mark.parametrize("status", [
        "planning", "running", "done", "failed", "cancelled", "waiting"
    ])
    def test_valid_statuses(self, status):
        self._insert(status)

    def test_invalid_status_raises(self):
        with db._conn() as c:
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(
                    "INSERT INTO work_packets (user_turn_id, status, created_at) "
                    "VALUES ('turn-x', 'NOPE', unixepoch())"
                )


# ---------------------------------------------------------------------------
# work_packet_steps.status
# CHECK (status IN ('pending','running','done','waiting','failed','skipped','cancelled'))
# ---------------------------------------------------------------------------

class TestWorkPacketStepsStatus:
    def _make_packet(self) -> int:
        with db._conn() as c:
            cur = c.execute(
                "INSERT INTO work_packets (user_turn_id, status, created_at) "
                "VALUES ('turn-s', 'running', unixepoch())"
            )
            return cur.lastrowid

    def _insert_step(self, packet_id: int, status: str, idx: int) -> None:
        with db._conn() as c:
            c.execute(
                "INSERT INTO work_packet_steps "
                "(packet_id, step_index, tool_name, status, created_at) "
                "VALUES (?, ?, 'tool', ?, unixepoch())",
                (packet_id, idx, status),
            )

    @pytest.mark.parametrize("status,idx", [
        ("pending", 0), ("running", 1), ("done", 2), ("waiting", 3),
        ("failed", 4), ("skipped", 5), ("cancelled", 6),
    ])
    def test_valid_statuses(self, status, idx):
        pid = self._make_packet()
        self._insert_step(pid, status, idx)

    def test_invalid_status_raises(self):
        pid = self._make_packet()
        with db._conn() as c:
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(
                    "INSERT INTO work_packet_steps "
                    "(packet_id, step_index, tool_name, status, created_at) "
                    "VALUES (?, 99, 'tool', 'BAD', unixepoch())",
                    (pid,),
                )


# ---------------------------------------------------------------------------
# Gap tests: tables WITHOUT CHECK constraints on status
# These document the gap — bad values are accepted, no IntegrityError raised.
# ---------------------------------------------------------------------------

class TestMissingConstraintsGap:
    """Negative-space tests: document that these tables lack CHECK enforcement."""

    def test_facts_status_no_check_constraint(self):
        """facts.status has no CHECK clause — any value is accepted by SQLite.

        This is a gap: the schema relies on application-level gating
        (insert_fact always passes 'active'; supersede/invalidate update directly).
        Marked xfail so CI stays green but the gap is visible in the report.
        """
        fid = db.insert_fact("u", "p", "o")
        with db._conn() as c:
            # This should raise IntegrityError if CHECK existed, but currently does not.
            c.execute("UPDATE facts SET status='bogus_value' WHERE id=?", (fid,))
        with db._conn() as c:
            row = c.execute("SELECT status FROM facts WHERE id=?", (fid,)).fetchone()
        # Gap: bad value was accepted.
        assert row["status"] == "bogus_value"

    def test_reminders_status_no_check_constraint(self):
        """reminders.status has no CHECK clause — any value is accepted by SQLite."""
        with db._conn() as c:
            cur = c.execute(
                "INSERT INTO reminders (fire_at, lead_minutes, text, status, created_at) "
                "VALUES (datetime('now', '+1 hour'), 0, 'test', 'bad_status', datetime('now'))"
            )
            rid = cur.lastrowid
        with db._conn() as c:
            row = c.execute("SELECT status FROM reminders WHERE id=?", (rid,)).fetchone()
        assert row["status"] == "bad_status"

    def test_proactive_events_status_no_check_constraint(self):
        """proactive_events.status (added via ALTER) has no CHECK clause."""
        with db._conn() as c:
            cur = c.execute(
                "INSERT INTO proactive_events "
                "(sent_at, source, pattern, payload_json, status) "
                "VALUES (datetime('now'), 'test', 'p', '{}', 'invalid_status')"
            )
            eid = cur.lastrowid
        with db._conn() as c:
            row = c.execute(
                "SELECT status FROM proactive_events WHERE id=?", (eid,)
            ).fetchone()
        assert row["status"] == "invalid_status"
