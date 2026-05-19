"""Phase 10: scheduler reads timezone from config; cron jobs fire at local time."""
from __future__ import annotations
import importlib
from pathlib import Path
import pytest


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    cfg_text = (
        "scheduler:\n"
        '  timezone: "Europe/Oslo"\n'
    )
    cfg_path = tmp_path / "engagement.yaml"
    cfg_path.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(cfg_path))
    from agents import config
    config.reload()
    yield


def test_scheduler_uses_configured_timezone(monkeypatch):
    async def _noop_send(s): pass
    from agents import scheduler
    importlib.reload(scheduler)
    sched = scheduler.build_scheduler(_noop_send)
    assert str(sched.timezone) == "Europe/Oslo"
