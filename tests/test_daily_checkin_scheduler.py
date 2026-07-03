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


def test_google_health_probe_job_registered():
    """Bug 1 follow-up: a periodic re-probe must exist so a token that dies
    mid-uptime (7-day Testing-mode expiry) is caught without a restart —
    post_init only probes once at startup."""
    from apscheduler.triggers.interval import IntervalTrigger

    from agents.scheduler import build_scheduler
    async def fake_send(s): return None
    sched = build_scheduler(fake_send)
    job = sched.get_job("google_health_probe")
    assert job is not None
    assert isinstance(job.trigger, IntervalTrigger)


def test_google_health_probe_interval_configurable(monkeypatch):
    from agents import config as cfg
    from agents.scheduler import build_scheduler
    orig_get = cfg.get
    monkeypatch.setattr(
        cfg, "get",
        lambda k, d=None: 7 if k == "google_health.probe_interval_minutes" else orig_get(k, d),
    )
    async def fake_send(s): return None
    sched = build_scheduler(fake_send)
    job = sched.get_job("google_health_probe")
    assert job.trigger.interval.total_seconds() == 7 * 60


@pytest.mark.asyncio
async def test_google_health_probe_job_writes_healthy_state(monkeypatch):
    from unittest.mock import AsyncMock

    from agents.scheduler import build_scheduler
    from storage import db

    async def fake_send(s): return None
    sched = build_scheduler(fake_send)
    job = sched.get_job("google_health_probe")

    import agents.google_health as _gh
    monkeypatch.setattr(_gh, "probe_google_token", AsyncMock(return_value=(True, "")))
    await job.func()
    assert db.runtime_get("calendar_heartbeat_healthy") == "1"


@pytest.mark.asyncio
async def test_google_health_probe_job_writes_unhealthy_state(monkeypatch):
    from unittest.mock import AsyncMock

    from agents.scheduler import build_scheduler
    from storage import db

    async def fake_send(s): return None
    sched = build_scheduler(fake_send)
    job = sched.get_job("google_health_probe")

    import agents.google_health as _gh
    monkeypatch.setattr(_gh, "probe_google_token", AsyncMock(return_value=(False, "invalid_grant")))
    await job.func()
    assert db.runtime_get("calendar_heartbeat_healthy") == "0:invalid_grant"
