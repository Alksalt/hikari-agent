"""Scheduler job registration: daily_brief must appear in the built scheduler.

Sprint 1: daily_brief (agents/daily_brief.py) replaced the old daily_checkin
scheduler job (agents/daily_checkin.py's should_fire_now/maybe_run_daily_checkin
are unchanged and still used internally by daily_brief for schedule-edit
parsing, but the module is no longer wired into the scheduler directly)."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    monkeypatch.setenv("HOME_TZ", "Europe/Berlin")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from agents import config
    config.reload()
    yield


def test_daily_brief_job_registered():
    from agents.scheduler import build_scheduler
    async def fake_send(s): return None
    sched = build_scheduler(fake_send)
    job_ids = [j.id for j in sched.get_jobs()]
    assert "daily_brief" in job_ids
    assert "daily_checkin" not in job_ids


def test_daily_brief_job_can_be_disabled(monkeypatch):
    from agents import config as cfg
    from agents.scheduler import build_scheduler
    # Patch the enabled flag
    orig_get = cfg.get
    monkeypatch.setattr(cfg, "get",
                        lambda k, d=None: False if k == "daily_brief.enabled" else orig_get(k, d))
    async def fake_send(s): return None
    sched = build_scheduler(fake_send)
    job_ids = [j.id for j in sched.get_jobs()]
    assert "daily_brief" not in job_ids
