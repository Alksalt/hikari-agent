"""Tests for decision_calibration_curve (db) and _format_calibration_surface (decision_log)."""
from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_resolved(predicted_p: float, outcome: int, resolved_days_ago: int = 1) -> int:
    """Insert a decision and immediately mark it resolved at a synthetic timestamp."""
    from storage import db
    resolved_at = (datetime.now(UTC) - timedelta(days=resolved_days_ago)).isoformat()
    did = db.decision_insert("test decision", predicted_p, "2026-01-01")
    with db._conn() as c:
        c.execute(
            "UPDATE decisions SET outcome = ?, resolved_at = ? WHERE id = ?",
            (outcome, resolved_at, did),
        )
    return did


# ---------------------------------------------------------------------------
# decision_calibration_curve tests
# ---------------------------------------------------------------------------

def test_curve_empty_when_no_decisions():
    from storage import db
    curve = db.decision_calibration_curve(window_days=90, buckets=5)
    # Returns 5 rows (one per bucket), all with n=0.
    assert len(curve) == 5
    assert all(b["n"] == 0 for b in curve)


def test_curve_groups_into_5_buckets():
    """Seed one resolved decision in each bucket and verify n=1 per bucket."""
    from storage import db
    # One decision per bucket: [0-20), [20-40), [40-60), [60-80), [80-100]
    # Use midpoints so membership is unambiguous.
    for p, outcome in [(0.1, 0), (0.3, 1), (0.5, 0), (0.7, 1), (0.9, 0)]:
        _insert_resolved(p, outcome)

    curve = db.decision_calibration_curve(window_days=90, buckets=5)
    assert len(curve) == 5
    for b in curve:
        assert b["n"] == 1, f"bucket [{b['bucket_low']}, {b['bucket_high']}] has n={b['n']}"


def test_curve_window_filter():
    """Decisions resolved outside window_days must be excluded."""
    from storage import db
    # Resolved 200 days ago — outside 90-day window.
    _insert_resolved(0.5, 1, resolved_days_ago=200)
    # Resolved 10 days ago — inside window.
    _insert_resolved(0.5, 0, resolved_days_ago=10)

    curve = db.decision_calibration_curve(window_days=90, buckets=5)
    # [40-60) bucket (predicted_p=0.5) should have n=1 (only the recent one).
    mid_bucket = curve[2]  # [0.4, 0.6)
    assert mid_bucket["n"] == 1


def test_curve_upper_bound_only_on_last_bucket():
    """predicted_p=0.8 should land in bucket [0.6, 0.8), not [0.8, 1.0]."""
    from storage import db
    _insert_resolved(0.8, 1)

    curve = db.decision_calibration_curve(window_days=90, buckets=5)
    # Bucket 3: [0.6, 0.8) — strict upper bound, so 0.8 belongs to bucket 4 [0.8, 1.0]
    bucket_60_80 = curve[3]  # lo=0.6, hi=0.8, upper_op="<"
    bucket_80_100 = curve[4]  # lo=0.8, hi=1.0, upper_op="<="

    assert bucket_60_80["n"] == 0, (
        f"0.8 should NOT be in [0.6, 0.8): got n={bucket_60_80['n']}"
    )
    assert bucket_80_100["n"] == 1, (
        f"0.8 should be in [0.8, 1.0]: got n={bucket_80_100['n']}"
    )


def test_curve_bucket_boundaries():
    """Verify bucket_low/bucket_high values are correctly spaced for 5 buckets."""
    from storage import db
    curve = db.decision_calibration_curve(window_days=90, buckets=5)
    width = 0.2
    for i, b in enumerate(curve):
        assert b["bucket_low"] == pytest.approx(i * width)
        assert b["bucket_high"] == pytest.approx((i + 1) * width)


def test_curve_mean_predicted_and_actual_rate():
    """Spot-check mean_predicted and actual_rate for a single-bucket scenario."""
    from storage import db
    # Two decisions in [0.2, 0.4): predicted 0.25 and 0.35, outcomes 1 and 0.
    _insert_resolved(0.25, 1)
    _insert_resolved(0.35, 0)

    curve = db.decision_calibration_curve(window_days=90, buckets=5)
    b = curve[1]  # [0.2, 0.4)
    assert b["n"] == 2
    assert b["mean_predicted"] == pytest.approx(0.30, abs=1e-6)
    assert b["actual_rate"] == pytest.approx(0.50, abs=1e-6)


# ---------------------------------------------------------------------------
# _format_calibration_surface tests
# ---------------------------------------------------------------------------

def _make_curve(buckets_data: list[tuple[float, float, int, float, float]]) -> list[dict]:
    """Build a curve list from (lo, hi, n, mean_predicted, actual_rate) tuples."""
    return [
        {
            "bucket_low": lo,
            "bucket_high": hi,
            "n": n,
            "mean_predicted": mp,
            "actual_rate": ar,
        }
        for lo, hi, n, mp, ar in buckets_data
    ]


def test_calibration_surface_picks_worst_gap():
    """Helper must pick the bucket with the largest |predicted - actual| gap."""
    from agents.decision_log import _format_calibration_surface

    curve = _make_curve([
        (0.0, 0.2, 5, 0.1, 0.1),   # gap = 0.0
        (0.2, 0.4, 5, 0.3, 0.3),   # gap = 0.0
        (0.4, 0.6, 5, 0.5, 0.2),   # gap = 0.3 ← worst
        (0.6, 0.8, 5, 0.7, 0.6),   # gap = 0.1
        (0.8, 1.0, 5, 0.9, 0.9),   # gap = 0.0
    ])
    result = _format_calibration_surface(curve)
    assert result is not None
    # The picked bucket is [0.4, 0.6) → "in the middle"
    assert "in the middle" in result
    assert "50%" in result  # round(0.5 * 100)
    assert "20%" in result  # round(0.2 * 100)


def test_calibration_surface_returns_none_below_threshold():
    """Gap < 0.2 → None (not significant enough to surface)."""
    from agents.decision_log import _format_calibration_surface

    curve = _make_curve([
        (0.0, 0.2, 5, 0.1, 0.05),  # gap = 0.05
        (0.2, 0.4, 5, 0.3, 0.25),  # gap = 0.05
        (0.4, 0.6, 5, 0.5, 0.45),  # gap = 0.05
        (0.6, 0.8, 5, 0.7, 0.65),  # gap = 0.05
        (0.8, 1.0, 5, 0.9, 0.85),  # gap = 0.05
    ])
    assert _format_calibration_surface(curve) is None


def test_calibration_surface_returns_none_when_n_too_low():
    """All buckets with n < 3 → None."""
    from agents.decision_log import _format_calibration_surface

    curve = _make_curve([
        (0.0, 0.2, 2, 0.1, 0.9),   # gap huge but n=2 < 3
        (0.2, 0.4, 1, 0.3, 0.0),   # gap huge but n=1 < 3
        (0.4, 0.6, 0, 0.0, 0.0),   # empty
        (0.6, 0.8, 2, 0.7, 0.0),   # gap huge but n=2 < 3
        (0.8, 1.0, 1, 0.9, 0.0),   # gap huge but n=1 < 3
    ])
    assert _format_calibration_surface(curve) is None


def test_calibration_surface_overconfident_up_high():
    """predicted 0.8, actual 0.4 → 'overconfident up high'."""
    from agents.decision_log import _format_calibration_surface

    curve = _make_curve([
        (0.0, 0.2, 0, 0.0, 0.0),
        (0.2, 0.4, 0, 0.0, 0.0),
        (0.4, 0.6, 0, 0.0, 0.0),
        (0.6, 0.8, 0, 0.0, 0.0),
        (0.8, 1.0, 5, 0.8, 0.4),   # gap = 0.4, bucket_low=0.8 >= 0.6
    ])
    result = _format_calibration_surface(curve)
    assert result is not None
    assert "overconfident" in result
    assert "up high" in result
    assert "80%" in result
    assert "40%" in result


def test_calibration_surface_underconfident_down_low():
    """predicted 0.2, actual 0.6 → 'underconfident down low'."""
    from agents.decision_log import _format_calibration_surface

    curve = _make_curve([
        (0.0, 0.2, 5, 0.2, 0.6),   # gap = 0.4, bucket_high=0.2 <= 0.4
        (0.2, 0.4, 0, 0.0, 0.0),
        (0.4, 0.6, 0, 0.0, 0.0),
        (0.6, 0.8, 0, 0.0, 0.0),
        (0.8, 1.0, 0, 0.0, 0.0),
    ])
    result = _format_calibration_surface(curve)
    assert result is not None
    assert "underconfident" in result
    assert "down low" in result
    assert "20%" in result
    assert "60%" in result


def test_calibration_surface_empty_curve():
    """Empty list → None (guard against edge case)."""
    from agents.decision_log import _format_calibration_surface
    assert _format_calibration_surface([]) is None


def test_calibration_surface_in_the_middle_zone():
    """Bucket [0.4, 0.6) → 'in the middle'."""
    from agents.decision_log import _format_calibration_surface

    curve = _make_curve([
        (0.0, 0.2, 0, 0.0, 0.0),
        (0.2, 0.4, 0, 0.0, 0.0),
        (0.4, 0.6, 4, 0.5, 0.1),   # gap = 0.4, mid bucket
        (0.6, 0.8, 0, 0.0, 0.0),
        (0.8, 1.0, 0, 0.0, 0.0),
    ])
    result = _format_calibration_surface(curve)
    assert result is not None
    assert "in the middle" in result
