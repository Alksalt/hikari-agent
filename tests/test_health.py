"""Sprint 6D — startup health probe tests."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.health import (
    _check_graph_outbox,
    _check_last_backup,
    _check_mcp_warm_pool,
    _check_media_outbox,
    _check_recent_log_errors,
    _check_scheduler,
    collect_startup_report,
    format_startup_digest,
    is_degraded,
    should_send_digest,
)

# ---------------------------------------------------------------------------
# is_degraded / format_startup_digest contracts
# ---------------------------------------------------------------------------

def test_is_degraded_false_when_all_green():
    report = {
        "db_integrity": {"ok": True, "value": "ok"},
        "scheduler_jobs": {"ok": True, "value": 6},
    }
    assert is_degraded(report) is False


def test_is_degraded_true_when_any_check_fails():
    report = {
        "db_integrity": {"ok": True, "value": "ok"},
        "oauth_google": {"ok": False, "value": "unhealthy", "reason": "invalid_grant"},
    }
    assert is_degraded(report) is True


def test_format_digest_all_green():
    report = {"db_integrity": {"ok": True, "value": "ok"}}
    out = format_startup_digest(report)
    assert "all green" in out
    assert len(out) <= 300


def test_format_digest_only_lists_bad_checks():
    report = {
        "db_integrity": {"ok": True, "value": "ok"},
        "scheduler_jobs": {"ok": False, "value": 0, "reason": "scheduler_not_in_bot_data"},
        "oauth_google": {"ok": False, "value": "unhealthy", "reason": "invalid_grant"},
    }
    out = format_startup_digest(report)
    assert "scheduler_jobs=scheduler_not_in_bot_data" in out
    assert "oauth_google=invalid_grant" in out
    # Healthy check must NOT appear
    assert "db_integrity" not in out
    assert len(out) <= 300


def test_format_digest_caps_at_300_chars():
    # Build many degraded checks so the formatted output would exceed 300 chars.
    report = {
        f"check_{i}": {"ok": False, "value": None, "reason": "x" * 60}
        for i in range(20)
    }
    out = format_startup_digest(report)
    assert len(out) <= 300
    assert out.endswith("…")


# ---------------------------------------------------------------------------
# should_send_digest env gating
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode,degraded,expected", [
    ("never", True, False),
    ("never", False, False),
    ("always", True, True),
    ("always", False, True),
    ("on_degrade", True, True),
    ("on_degrade", False, False),
    ("", True, True),     # unknown → on_degrade
    ("", False, False),
])
def test_should_send_digest_modes(mode: str, degraded: bool, expected: bool):
    report = {"x": {"ok": not degraded, "value": "ok"}}
    assert should_send_digest(report, mode=mode if mode else "on_degrade") is expected


# ---------------------------------------------------------------------------
# Individual check happy / degraded branches
# ---------------------------------------------------------------------------

def test_scheduler_check_none_is_degraded():
    result = _check_scheduler(None)
    assert result.ok is False
    assert result.reason == "scheduler_not_in_bot_data"


def test_scheduler_check_empty_jobs_is_degraded():
    fake = MagicMock()
    fake.get_jobs.return_value = []
    result = _check_scheduler(fake)
    assert result.ok is False
    assert result.value == 0


def test_scheduler_check_with_jobs_ok():
    fake = MagicMock()
    fake.get_jobs.return_value = [MagicMock(id="job_a"), MagicMock(id="job_b")]
    result = _check_scheduler(fake)
    assert result.ok is True
    assert result.value == 2


def test_mcp_warm_pool_handles_exception():
    with patch("agents.mcp_manager.MANAGER") as mock_mgr:
        mock_mgr.warm_servers.side_effect = RuntimeError("pool dead")
        result = _check_mcp_warm_pool()
    assert result.ok is False
    assert "exception:RuntimeError" in (result.reason or "")


def test_graph_outbox_under_threshold():
    with patch("storage.db.graph_outbox_pending", return_value=[{"id": i} for i in range(3)]):
        with patch("storage.db.graph_outbox_failed_stats", return_value={"count": 0, "last_error": None}):
            result = _check_graph_outbox()
    assert result.ok is True
    assert result.value == {"pending": 3, "failed": 0}


def test_graph_outbox_over_threshold_degraded():
    # The check uses limit = _OUTBOX_PENDING_WARN + 1 = 51
    with patch("storage.db.graph_outbox_pending", return_value=[{"id": i} for i in range(51)]):
        with patch("storage.db.graph_outbox_failed_stats", return_value={"count": 0, "last_error": None}):
            result = _check_graph_outbox()
    assert result.ok is False
    assert "backlog>50" in (result.reason or "")


def test_last_backup_missing_dir_degraded(tmp_path: Path):
    with patch("agents.health._BACKUP_DIR", tmp_path / "does-not-exist"):
        result = _check_last_backup()
    assert result.ok is False
    assert result.reason == "backup_dir_missing"


def test_last_backup_no_files_degraded(tmp_path: Path):
    with patch("agents.health._BACKUP_DIR", tmp_path):
        result = _check_last_backup()
    assert result.ok is False
    assert result.reason == "no_backups_found"


def test_last_backup_fresh_ok(tmp_path: Path):
    f = tmp_path / "hikari-20260524.db"
    f.write_bytes(b"x")
    with patch("agents.health._BACKUP_DIR", tmp_path):
        result = _check_last_backup()
    assert result.ok is True
    assert result.value < 1.0  # fresh, less than 1h old


def test_last_backup_picks_up_tar_age(tmp_path: Path):
    """Fix 1: _check_last_backup must detect .tar.age files (Sprint 7F format)."""
    f = tmp_path / "hikari-20260524.tar.age"
    f.write_bytes(b"encrypted")
    with patch("agents.health._BACKUP_DIR", tmp_path):
        result = _check_last_backup()
    assert result.ok is True
    assert result.value < 1.0  # fresh


def test_last_backup_tar_age_preferred_over_legacy_db(tmp_path: Path):
    """Fix 1: when both .tar.age and .db exist, .tar.age is used (break after first pattern)."""
    import os
    age_file = tmp_path / "hikari-20260524.tar.age"
    age_file.write_bytes(b"encrypted")
    # A stale .db file that would appear degraded if selected
    db_file = tmp_path / "hikari-20260520.db"
    db_file.write_bytes(b"x")
    old = time.time() - 48 * 3600
    os.utime(db_file, (old, old))
    with patch("agents.health._BACKUP_DIR", tmp_path):
        result = _check_last_backup()
    # Should be fresh — the .tar.age is recent, not the old .db
    assert result.ok is True


def test_last_backup_stale_degraded(tmp_path: Path):
    f = tmp_path / "hikari-20260520.db"
    f.write_bytes(b"x")
    # Back-date by 48h
    old = time.time() - 48 * 3600
    import os
    os.utime(f, (old, old))
    with patch("agents.health._BACKUP_DIR", tmp_path):
        result = _check_last_backup()
    assert result.ok is False
    assert "stale>30h" in (result.reason or "")


def test_log_errors_missing_file_ok(tmp_path: Path):
    result = _check_recent_log_errors(log_path=tmp_path / "nope.log")
    assert result.ok is True


def test_log_errors_under_threshold(tmp_path: Path):
    log = tmp_path / "hikari.log"
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    log.write_text(f"{now_str} INFO ok\n{now_str} ERROR boom\n")
    result = _check_recent_log_errors(log_path=log)
    assert result.ok is True
    assert result.value == 1


def test_log_errors_over_threshold_degraded(tmp_path: Path):
    log = tmp_path / "hikari.log"
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"{now_str} ERROR fail {i}\n" for i in range(10)]
    log.write_text("".join(lines))
    result = _check_recent_log_errors(log_path=log)
    assert result.ok is False
    assert "errors>5/hr" in (result.reason or "")


def test_log_errors_ignores_old_timestamps(tmp_path: Path):
    log = tmp_path / "hikari.log"
    # Errors from 5 hours ago — should NOT count.
    log.write_text("2020-01-01 00:00:00 ERROR ancient\n" * 20)
    result = _check_recent_log_errors(log_path=log, window_sec=3600)
    assert result.ok is True
    assert result.value == 0


# ---------------------------------------------------------------------------
# collect_startup_report integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_collect_startup_report_returns_all_keys():
    fake_sched = MagicMock()
    fake_sched.get_jobs.return_value = [MagicMock(id="x")]
    with patch("agents.google_health.probe_google_token", new=AsyncMock(return_value=(True, ""))):
        report = await collect_startup_report(scheduler=fake_sched)
    assert set(report.keys()) == {
        "db_integrity",
        "scheduler_jobs",
        "mcp_warm_pool",
        "oauth_google",
        "graph_outbox_pending",
        "media_outbox_pending",
        "last_backup_age_h",
        "log_recent_errors",
    }
    for check in report.values():
        assert "ok" in check
        assert "value" in check


@pytest.mark.asyncio
async def test_collect_startup_report_uses_prefetched_oauth():
    """When oauth_google_prefetched is passed, probe_google_token must NOT be called."""
    fake_sched = MagicMock()
    fake_sched.get_jobs.return_value = [MagicMock(id="x")]
    probe_mock = AsyncMock(return_value=(True, ""))
    with patch("agents.google_health.probe_google_token", probe_mock):
        report = await collect_startup_report(
            scheduler=fake_sched,
            oauth_google_prefetched=(False, "invalid_grant"),
        )
    probe_mock.assert_not_called()
    assert report["oauth_google"]["ok"] is False
    assert report["oauth_google"]["reason"] == "invalid_grant"


@pytest.mark.asyncio
async def test_collect_startup_report_never_raises():
    """Even if every dependency blows up, the collector returns a dict."""
    fake_sched = MagicMock()
    fake_sched.get_jobs.side_effect = RuntimeError("scheduler dead")
    with patch("agents.google_health.probe_google_token", new=AsyncMock(side_effect=RuntimeError("oauth dead"))), \
         patch("storage.db.graph_outbox_pending", side_effect=RuntimeError("outbox dead")):
        report = await collect_startup_report(scheduler=fake_sched)
    assert isinstance(report, dict)
    assert report["scheduler_jobs"]["ok"] is False
    assert report["oauth_google"]["ok"] is False
    assert report["graph_outbox_pending"]["ok"] is False


# ---------------------------------------------------------------------------
# media_outbox check
# ---------------------------------------------------------------------------

def test_media_outbox_under_threshold_ok():
    with patch("storage.db.media_outbox_stats", return_value={"pending": 5, "sent": 3, "failed": 0, "aborted": 0}):
        result = _check_media_outbox()
    assert result.ok is True
    assert result.value == 5


def test_media_outbox_over_threshold_degraded():
    with patch("storage.db.media_outbox_stats", return_value={"pending": 25, "sent": 10, "failed": 2, "aborted": 1}):
        result = _check_media_outbox()
    assert result.ok is False
    assert result.value == 25
    assert result.reason is not None


def test_media_outbox_exception_returns_degraded():
    with patch("storage.db.media_outbox_stats", side_effect=RuntimeError("db dead")):
        result = _check_media_outbox()
    assert result.ok is False


@pytest.mark.asyncio
async def test_collect_startup_report_includes_media_outbox_key():
    """collect_startup_report must include 'media_outbox_pending' key."""
    fake_sched = MagicMock()
    fake_sched.get_jobs.return_value = [MagicMock(id="x")]
    with patch("agents.google_health.probe_google_token", new=AsyncMock(return_value=(True, ""))), \
         patch("storage.db.media_outbox_stats", return_value={"pending": 0, "sent": 0, "failed": 0, "aborted": 0}):
        report = await collect_startup_report(scheduler=fake_sched)
    assert "media_outbox_pending" in report
    assert report["media_outbox_pending"]["ok"] is True
