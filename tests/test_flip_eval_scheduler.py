"""Scheduler registration + alert behavior for the weekly flip eval."""
from __future__ import annotations

import importlib
from unittest.mock import AsyncMock, patch

import pytest


def _build(monkeypatch, enabled: bool):
    from agents import config as cfg
    orig_get = cfg.get
    def fake_get(key, default=None):
        if key == "flip_eval.enabled":
            return enabled
        return orig_get(key, default)
    monkeypatch.setattr(cfg, "get", fake_get)
    from agents import scheduler
    importlib.reload(scheduler)
    async def _noop_send(s): pass
    return scheduler.build_scheduler(_noop_send)


def test_flip_eval_job_registered_when_enabled(monkeypatch):
    sched = _build(monkeypatch, enabled=True)
    assert "flip_eval" in [j.id for j in sched.get_jobs()]


def test_flip_eval_trigger_fields(monkeypatch):
    """Pin the config-driven schedule (sun 21:00 defaults) and the tight
    misfire grace — 600s so a restart late in the 20:00-21:00 window can't
    fire a misfired drift_canary and the on-time flip eval back to back."""
    sched = _build(monkeypatch, enabled=True)
    job = next(j for j in sched.get_jobs() if j.id == "flip_eval")
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["day_of_week"] == "sun"
    assert fields["hour"] == "21"
    assert fields["minute"] == "0"
    assert job.misfire_grace_time == 600


def test_flip_eval_job_absent_when_disabled(monkeypatch):
    sched = _build(monkeypatch, enabled=False)
    assert "flip_eval" not in [j.id for j in sched.get_jobs()]


@pytest.mark.asyncio
async def test_flip_eval_job_alerts_only_on_gate_failure():
    from agents.scheduler import _run_flip_eval_job

    sent: list[str] = []
    async def send_text(s): sent.append(s)

    passing = {"run_id": 1, "bank_version": "v1", "items": [],
               "regressive_rate": 0.0, "anchor_flips": 0, "n_judged": 9}
    with patch("evals.flip.harness.run_flip_eval",
               new=AsyncMock(return_value=passing)):
        await _run_flip_eval_job(send_text)
    assert sent == []

    failing = {**passing, "regressive_rate": 0.5,
               "items": [], "anchor_flips": 1}
    with patch("evals.flip.harness.run_flip_eval",
               new=AsyncMock(return_value=failing)):
        await _run_flip_eval_job(send_text)
    assert len(sent) == 1
    assert sent[0].startswith("⚠ flip eval:")
