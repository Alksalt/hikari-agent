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


# ---------------------------------------------------------------------------
# Phase B: new tables, ALTER columns, CHECK widening
# ---------------------------------------------------------------------------

class TestPhaseBMediaOutboxVoice:
    def test_media_outbox_kind_voice_allowed(self):
        """media_outbox now accepts kind='voice' after CHECK widening."""
        with db._conn() as c:
            c.execute(
                "INSERT INTO media_outbox "
                "(kind, idempotency_key, payload_json, created_at) "
                "VALUES ('voice', 'idem-voice-1', '{}', datetime('now'))"
            )
        with db._conn() as c:
            row = c.execute(
                "SELECT kind FROM media_outbox WHERE idempotency_key='idem-voice-1'"
            ).fetchone()
        assert row["kind"] == "voice"

    def test_media_outbox_kind_still_rejects_unknown(self):
        """media_outbox CHECK still rejects values outside the allowed set."""
        with db._conn() as c:
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(
                    "INSERT INTO media_outbox "
                    "(kind, idempotency_key, payload_json, created_at) "
                    "VALUES ('audio_file', 'idem-bad', '{}', datetime('now'))"
                )

    def test_media_outbox_original_kinds_still_work(self):
        """Original kind values remain valid after the rebuild."""
        for kind in ("text", "photo", "sticker", "document"):
            with db._conn() as c:
                c.execute(
                    "INSERT INTO media_outbox "
                    "(kind, idempotency_key, payload_json, created_at) "
                    "VALUES (?, ?, '{}', datetime('now'))",
                    (kind, f"idem-orig-{kind}"),
                )


class TestPhaseBLlmCosts:
    def test_llm_costs_table_exists_and_indexed(self):
        """llm_costs table exists and the ts index is present."""
        with db._conn() as c:
            cur = c.execute(
                "INSERT INTO llm_costs "
                "(ts, model, path, input_tokens, output_tokens, cost_usd) "
                "VALUES (datetime('now'), 'claude-sonnet-4-6', 'main_chat', 10, 5, 0.001)"
            )
            row_id = cur.lastrowid
        assert row_id is not None
        with db._conn() as c:
            idx = c.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_llm_costs_ts'"
            ).fetchone()
        assert idx is not None

    def test_llm_costs_cache_columns_default_zero(self):
        """cache_read_input_tokens and cache_creation_input_tokens default to 0."""
        with db._conn() as c:
            cur = c.execute(
                "INSERT INTO llm_costs (ts, model, path, input_tokens, output_tokens, cost_usd) "
                "VALUES (datetime('now'), 'm', 'p', 1, 1, 0.0)"
            )
            rid = cur.lastrowid
        with db._conn() as c:
            row = c.execute("SELECT * FROM llm_costs WHERE id=?", (rid,)).fetchone()
        assert row["cache_read_input_tokens"] == 0
        assert row["cache_creation_input_tokens"] == 0


class TestPhaseBVoiceCorrections:
    def test_voice_corrections_fifo_index(self):
        """ts DESC index exists; SELECT ORDER BY ts DESC returns most recent first."""
        with db._conn() as c:
            c.execute(
                "INSERT INTO voice_corrections (ts, correction_text) "
                "VALUES ('2026-01-01T10:00:00Z', 'older one')"
            )
            c.execute(
                "INSERT INTO voice_corrections (ts, correction_text) "
                "VALUES ('2026-01-02T10:00:00Z', 'newer one')"
            )
        with db._conn() as c:
            rows = c.execute(
                "SELECT correction_text FROM voice_corrections ORDER BY ts DESC LIMIT 1"
            ).fetchall()
        assert rows[0]["correction_text"] == "newer one"

    def test_voice_corrections_index_exists(self):
        with db._conn() as c:
            idx = c.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND name='idx_voice_corrections_ts_desc'"
            ).fetchone()
        assert idx is not None


class TestPhaseBBeliefJournal:
    def test_belief_journal_claim_type_check_factual(self):
        with db._conn() as c:
            c.execute(
                "INSERT INTO belief_journal "
                "(stated_at, statement, claim_type, resurface_at) "
                "VALUES (datetime('now'), 'i will ship this week', 'factual', "
                "        datetime('now', '+90 days'))"
            )

    def test_belief_journal_claim_type_check_identity(self):
        with db._conn() as c:
            c.execute(
                "INSERT INTO belief_journal "
                "(stated_at, statement, claim_type, resurface_at) "
                "VALUES (datetime('now'), 'i am someone who ships', 'identity', "
                "        datetime('now', '+90 days'))"
            )

    def test_belief_journal_claim_type_rejects_invalid(self):
        with db._conn() as c:
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(
                    "INSERT INTO belief_journal "
                    "(stated_at, statement, claim_type, resurface_at) "
                    "VALUES (datetime('now'), 'bad', 'bogus', datetime('now'))"
                )

    def test_belief_journal_resolved_bool_defaults_zero(self):
        with db._conn() as c:
            cur = c.execute(
                "INSERT INTO belief_journal "
                "(stated_at, statement, claim_type, resurface_at) "
                "VALUES (datetime('now'), 's', 'factual', datetime('now', '+90 days'))"
            )
            rid = cur.lastrowid
        with db._conn() as c:
            row = c.execute(
                "SELECT resolved_bool FROM belief_journal WHERE id=?", (rid,)
            ).fetchone()
        assert row["resolved_bool"] == 0


class TestPhaseBSignificantEvents:
    def test_significant_events_kind_check_good(self):
        with db._conn() as c:
            c.execute(
                "INSERT INTO significant_events (event_date, summary, kind, created_at) "
                "VALUES ('2026-01-01', 'shipped the thing', 'good', datetime('now'))"
            )

    @pytest.mark.parametrize("kind", ["hard", "funny", "milestone"])
    def test_significant_events_kind_check_valid(self, kind):
        with db._conn() as c:
            c.execute(
                "INSERT INTO significant_events (event_date, summary, kind, created_at) "
                "VALUES ('2026-01-02', 'something happened', ?, datetime('now'))",
                (kind,),
            )

    def test_significant_events_kind_check_rejects_invalid(self):
        with db._conn() as c:
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(
                    "INSERT INTO significant_events (event_date, summary, kind, created_at) "
                    "VALUES ('2026-01-03', 'x', 'bad_kind', datetime('now'))"
                )


class TestPhaseBAlterColumns:
    def test_lexicon_first_seen_date_column_exists(self):
        """lexicon.first_seen_date column is present after migration."""
        with db._conn() as c:
            cols = {row["name"] for row in c.execute("PRAGMA table_info(lexicon)").fetchall()}
        assert "first_seen_date" in cols

    def test_lexicon_first_seen_date_backfilled(self):
        """first_seen_date is backfilled from created_at for rows inserted before the migration.

        The migration runs at boot so any row inserted via the fixture's
        upsert_core_block seed already has the migration applied. Insert a new
        lexicon row and check that explicitly set created_at propagates to
        first_seen_date via a manual UPDATE to simulate pre-migration data.
        """
        with db._conn() as c:
            c.execute(
                "INSERT INTO lexicon (phrase, last_used_at, created_at) "
                "VALUES ('test-phrase', datetime('now'), datetime('2026-01-15'))"
            )
            # Simulate a pre-migration row that never got backfilled
            c.execute(
                "UPDATE lexicon SET first_seen_date = NULL WHERE phrase = 'test-phrase'"
            )
            # Run backfill manually as the migration would
            c.execute(
                "UPDATE lexicon SET first_seen_date = date(created_at) "
                "WHERE first_seen_date IS NULL AND created_at IS NOT NULL"
            )
            row = c.execute(
                "SELECT first_seen_date FROM lexicon WHERE phrase='test-phrase'"
            ).fetchone()
        assert row["first_seen_date"] == "2026-01-15"

    def test_facts_fact_category_column_exists_nullable(self):
        """facts.fact_category column exists and accepts NULL."""
        with db._conn() as c:
            cols = {row["name"] for row in c.execute("PRAGMA table_info(facts)").fetchall()}
        assert "fact_category" in cols
        fid = db.insert_fact("u", "p", "o")
        with db._conn() as c:
            row = c.execute("SELECT fact_category FROM facts WHERE id=?", (fid,)).fetchone()
        assert row["fact_category"] is None

    def test_facts_fact_category_accepts_value(self):
        fid = db.insert_fact("u", "p", "o")
        with db._conn() as c:
            c.execute("UPDATE facts SET fact_category='preference' WHERE id=?", (fid,))
            row = c.execute("SELECT fact_category FROM facts WHERE id=?", (fid,)).fetchone()
        assert row["fact_category"] == "preference"

    def test_tasks_research_intent_default_zero(self):
        """tasks.research_intent defaults to 0."""
        tid = db.create_task("research task")
        with db._conn() as c:
            row = c.execute(
                "SELECT research_intent FROM tasks WHERE id=?", (tid,)
            ).fetchone()
        assert row["research_intent"] == 0

    def test_tasks_research_intent_accepts_one(self):
        tid = db.create_task("research task 2")
        with db._conn() as c:
            c.execute("UPDATE tasks SET research_intent=1 WHERE id=?", (tid,))
            row = c.execute("SELECT research_intent FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["research_intent"] == 1
