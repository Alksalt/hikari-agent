"""Phase 8 — morning dispatch markdown emitted by daily reflection.

Covers:
  - File written to <vault>/projects/hikari-agent/morning_dispatch/<date>.md
  - Sections present: traffic, drift, lexicon, noticings, open loops, feedback
  - Drift section honors None drift_avg cleanly
  - Lexicon / noticings / loops list empty-state copy
  - Re-running the same date overwrites (idempotent)
  - Failure inside the helper does NOT raise (best-effort)
"""

from __future__ import annotations

import importlib
from datetime import date, datetime
from pathlib import Path

import pytest

from agents import config, reflection
from storage import db


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    config.reload()

    # Redirect VAULT_ROOT to a tmp dir for these tests.
    vault = tmp_path / "vault"
    vault.mkdir()
    import tools.wiki as wiki_mod
    monkeypatch.setattr(wiki_mod, "VAULT_ROOT", vault)
    yield vault


def test_writes_markdown_at_expected_path(_isolated):
    vault = _isolated
    today = date(2026, 5, 19)
    target = reflection._write_morning_dispatch(
        today=today, drift_avg=None, drift_below=0,
    )
    assert target is not None
    expected = (
        vault / "projects" / "hikari-agent" / "morning_dispatch"
        / f"morning_dispatch_{today.isoformat()}.md"
    )
    assert target == expected
    assert expected.exists()
    body = expected.read_text(encoding="utf-8")
    assert "# Morning dispatch" in body
    assert "## traffic" in body
    assert "## persona drift" in body
    assert "## lexicon top" in body
    assert "## new noticings" in body
    assert "## open loops" in body
    assert "## ground-truth feedback" in body


def test_drift_section_flags_low_average(_isolated):
    today = date(2026, 5, 19)
    target = reflection._write_morning_dispatch(
        today=today, drift_avg=0.45, drift_below=5,
    )
    body = target.read_text(encoding="utf-8")
    assert "0.45" in body
    assert "⚠️" in body
    assert "below-threshold samples: **5**" in body


def test_drift_section_no_warning_when_healthy(_isolated):
    today = date(2026, 5, 19)
    target = reflection._write_morning_dispatch(
        today=today, drift_avg=0.85, drift_below=0,
    )
    body = target.read_text(encoding="utf-8")
    assert "0.85" in body
    assert "⚠️" not in body


def test_lexicon_section_lists_top_entries(_isolated):
    today = date(2026, 5, 19)
    db.lexicon_record("attention sinks", source="mutual")
    db.lexicon_record("attention sinks")  # bump weight
    db.lexicon_record("cold rice", source="user_coined")
    target = reflection._write_morning_dispatch(
        today=today, drift_avg=None, drift_below=0,
    )
    body = target.read_text(encoding="utf-8")
    assert "attention sinks" in body
    assert "cold rice" in body


def test_open_loops_with_ages(_isolated):
    today = date(2026, 5, 19)
    db.create_task("read the cabbage paper")
    target = reflection._write_morning_dispatch(
        today=today, drift_avg=None, drift_below=0,
    )
    body = target.read_text(encoding="utf-8")
    assert "read the cabbage paper" in body
    # New task ⇒ age=0d.
    assert "(0d old)" in body


def test_empty_state_uses_friendly_copy(_isolated):
    today = date(2026, 5, 19)
    target = reflection._write_morning_dispatch(
        today=today, drift_avg=None, drift_below=0,
    )
    body = target.read_text(encoding="utf-8")
    assert "no samples this week." in body
    assert "(nothing promoted yet.)" in body
    assert "(none.)" in body
    assert "(clean board.)" in body
    # D-3 wired feedback_compare_to_drift; empty-state path returns counts of zero.
    assert ("no 👍/👎 reactions logged yet" in body
            or "agree=0, disagree=0" in body)


def test_idempotent_overwrites_same_date(_isolated):
    today = date(2026, 5, 19)
    first = reflection._write_morning_dispatch(
        today=today, drift_avg=0.9, drift_below=0,
    )
    second = reflection._write_morning_dispatch(
        today=today, drift_avg=0.3, drift_below=4,
    )
    assert first == second
    body = first.read_text(encoding="utf-8")
    # Latest run wins.
    assert "0.30" in body
    assert "0.9" not in body or "0.30" in body  # overwritten


def test_message_count_window_covers_prior_24h(tmp_path, monkeypatch, _isolated):
    today = date(2026, 5, 19)
    yesterday_iso = (datetime(2026, 5, 18, 12, 0, 0)).isoformat()
    today_morn = (datetime(2026, 5, 19, 8, 0, 0)).isoformat()
    older = (datetime(2026, 5, 17, 12, 0, 0)).isoformat()

    # Insert messages straight into the table with controlled timestamps.
    with db._conn() as c:
        c.execute(
            "INSERT INTO messages (role, content, ts) VALUES (?, ?, ?)",
            ("user", "yesterday hello", yesterday_iso),
        )
        c.execute(
            "INSERT INTO messages (role, content, ts) VALUES (?, ?, ?)",
            ("assistant", "yesterday reply", yesterday_iso),
        )
        c.execute(
            "INSERT INTO messages (role, content, ts) VALUES (?, ?, ?)",
            ("user", "morning ping", today_morn),
        )
        c.execute(
            "INSERT INTO messages (role, content, ts) VALUES (?, ?, ?)",
            ("user", "ancient", older),
        )

    target = reflection._write_morning_dispatch(
        today=today, drift_avg=None, drift_below=0,
    )
    body = target.read_text(encoding="utf-8")
    # Window is [yesterday 00:00, today 00:00). Should count 2 yesterday msgs;
    # exclude the morning_ping (after the window) and the older one.
    assert "**2**" in body


def test_helper_tolerates_db_failure(monkeypatch, _isolated):
    """If lexicon/noticings/loops blow up, the file still writes (with empty
    sections). Best-effort guarantee."""
    today = date(2026, 5, 19)

    def boom(*a, **kw):
        raise RuntimeError("simulated db blow up")

    monkeypatch.setattr(reflection.db, "lexicon_top", boom)
    monkeypatch.setattr(reflection.db, "open_tasks", boom)

    target = reflection._write_morning_dispatch(
        today=today, drift_avg=None, drift_below=0,
    )
    assert target is not None
    body = target.read_text(encoding="utf-8")
    assert "(nothing promoted yet.)" in body
    assert "(clean board.)" in body


def test_new_noticings_filter(_isolated):
    today = date(2026, 5, 19)
    # Old noticing → outside the window.
    with db._conn() as c:
        c.execute(
            "INSERT INTO noticings (signal, summary, created_at, surfaced_at) "
            "VALUES (?, ?, ?, NULL)",
            ("topic_dropped", "stopped mentioning meria", "2026-04-01T00:00:00"),
        )
    # Fresh noticing → in the window.
    db.noticing_record(signal="cadence_shift", summary="goes quiet later")
    target = reflection._write_morning_dispatch(
        today=today, drift_avg=None, drift_below=0,
    )
    body = target.read_text(encoding="utf-8")
    assert "goes quiet later" in body
    assert "stopped mentioning meria" not in body
