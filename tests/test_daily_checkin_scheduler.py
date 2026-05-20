"""Scheduler job registration: daily_checkin must appear in the built scheduler."""
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


def test_daily_checkin_job_registered():
    from agents.scheduler import build_scheduler
    async def fake_send(s): return None
    sched = build_scheduler(fake_send)
    job_ids = [j.id for j in sched.get_jobs()]
    assert "daily_checkin" in job_ids


def test_daily_checkin_job_can_be_disabled(monkeypatch):
    from agents import config as cfg
    from agents.scheduler import build_scheduler
    # Patch the enabled flag
    orig_get = cfg.get
    monkeypatch.setattr(cfg, "get",
                        lambda k, d=None: False if k == "daily_checkin.enabled" else orig_get(k, d))
    async def fake_send(s): return None
    sched = build_scheduler(fake_send)
    job_ids = [j.id for j in sched.get_jobs()]
    assert "daily_checkin" not in job_ids
