"""Job-search handoff consumer tests (phase 3 wiring, 2026-07-09).

No DB needed — the module only touches the handoff file + config. Config is
monkeypatched at the ``agents.config.get`` level so no YAML edits are required.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from agents import config, job_handoff

HEADER = ("<!-- append-only handoff written by autoscan (job-search); consumed "
          "by hikari/daily-checkin; do not hand-edit lines, mark them "
          "processed instead. -->\n")


def _stamp(hours_ago: float) -> str:
    return (datetime.now() - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M")


@pytest.fixture
def handoff_file(tmp_path: Path, monkeypatch):
    path = tmp_path / "job_search_handoff.md"
    settings = {
        "job_handoff.enabled": True,
        "job_handoff.path": str(path),
        "job_handoff.max_entries": 2,
        "job_handoff.max_age_hours": 72,
    }
    real_get = config.get
    monkeypatch.setattr(
        config, "get",
        lambda key, default=None: settings.get(key, real_get(key, default)),
    )
    return path


def test_missing_file_returns_empty(handoff_file):
    assert job_handoff.pull_unprocessed() == []


def test_pull_parses_entries_details_and_caps(handoff_file):
    handoff_file.write_text(
        HEADER
        + f"- [{_stamp(1)}] hot: 2 nye SØK-leads — status: unprocessed\n"
        + "    - 85 · Applikasjonsanalytiker — Helseplattformen\n"
        + "    - 78 · Data Engineer — Sensio\n"
        + f"- [{_stamp(2)}] frist: Østensjø ≤3 dager — status: unprocessed\n"
        + f"- [{_stamp(3)}] digest: ukesoppsummering — status: unprocessed\n"
    )
    entries = job_handoff.pull_unprocessed()
    assert len(entries) == 2  # capped by max_entries
    assert entries[0]["summary"] == "hot: 2 nye SØK-leads"
    assert entries[0]["details"] == [
        "85 · Applikasjonsanalytiker — Helseplattformen",
        "78 · Data Engineer — Sensio",
    ]
    assert "Helseplattformen" in job_handoff.format_lines(entries)


def test_pull_skips_processed_and_stale(handoff_file):
    handoff_file.write_text(
        HEADER
        + f"- [{_stamp(1)}] hot: fresh lead — status: processed 2026-07-08\n"
        + f"- [{_stamp(200)}] hot: ancient lead — status: unprocessed\n"
        + f"- [{_stamp(2)}] frist: current — status: unprocessed\n"
    )
    entries = job_handoff.pull_unprocessed()
    assert [e["summary"] for e in entries] == ["frist: current"]


def test_mark_processed_survives_producer_append(handoff_file):
    line = f"- [{_stamp(1)}] hot: lead — status: unprocessed"
    handoff_file.write_text(HEADER + line + "\n")
    entries = job_handoff.pull_unprocessed()
    assert len(entries) == 1
    # producer appends between pull and mark (append-only contract)
    appended = f"- [{_stamp(0)}] digest: nyere — status: unprocessed"
    handoff_file.write_text(handoff_file.read_text() + appended + "\n")
    job_handoff.mark_processed(entries)
    text = handoff_file.read_text()
    assert "hot: lead — status: processed" in text
    assert appended in text  # append preserved
    assert job_handoff.pull_unprocessed()[0]["summary"] == "digest: nyere"


def test_disabled_returns_empty(handoff_file, monkeypatch):
    handoff_file.write_text(
        HEADER + f"- [{_stamp(1)}] hot: x — status: unprocessed\n")
    settings = {"job_handoff.enabled": False}
    real_get = config.get
    monkeypatch.setattr(
        config, "get",
        lambda key, default=None: settings.get(key, real_get(key, default)),
    )
    assert job_handoff.pull_unprocessed() == []
