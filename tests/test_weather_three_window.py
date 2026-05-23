"""Tests for _slice_window and _consensus helpers."""
from __future__ import annotations


def _make_hourly(n_hours: int = 24, base_temp: float = 15.0) -> dict:
    """24-hour hourly fixture with predictable values per hour."""
    times = [f"2026-05-22T{h:02d}:00" for h in range(n_hours)]
    return {
        "time": times,
        "temp_c": [base_temp + h * 0.1 for h in range(n_hours)],
        "feels_c": [base_temp - 2.0 + h * 0.1 for h in range(n_hours)],
        "precip_prob_pct": [h * 2 for h in range(n_hours)],
        "weather_code": [2 if h < 12 else 3 for h in range(n_hours)],
        "cloud_cover_pct": [30 + h for h in range(n_hours)],
    }


def test_slice_window_returns_medians():
    import statistics

    from tools.weather._shared import _slice_window

    hourly = _make_hourly()

    morning = _slice_window(hourly, 7, 10)
    assert morning, "morning window should not be empty"

    # Hours 7, 8, 9
    expected_temps = [15.0 + h * 0.1 for h in [7, 8, 9]]
    assert morning["temp_c"] == round(statistics.median(expected_temps), 1)

    expected_feels = [15.0 - 2.0 + h * 0.1 for h in [7, 8, 9]]
    assert morning["feels_c"] == round(statistics.median(expected_feels), 1)

    expected_precip = [h * 2 for h in [7, 8, 9]]
    assert morning["precip_prob_pct"] == round(statistics.median(expected_precip))

    # weather_code for hours 7,8,9 is all 2 (< 12)
    assert morning["weather_code"] == 2

    expected_cloud = [30 + h for h in [7, 8, 9]]
    assert morning["cloud_cover_pct"] == round(statistics.median(expected_cloud))


def test_slice_window_midday():
    import statistics

    from tools.weather._shared import _slice_window

    hourly = _make_hourly()
    midday = _slice_window(hourly, 12, 15)
    assert midday
    # Hours 12, 13, 14
    expected_temps = [15.0 + h * 0.1 for h in [12, 13, 14]]
    assert midday["temp_c"] == round(statistics.median(expected_temps), 1)


def test_slice_window_empty_when_no_hours():
    from tools.weather._shared import _slice_window

    hourly = _make_hourly(n_hours=5)  # only hours 0-4, no morning/midday/evening
    assert _slice_window(hourly, 7, 10) == {}
    assert _slice_window(hourly, 12, 15) == {}
    assert _slice_window(hourly, 18, 21) == {}


def test_consensus_flags_temp_disagreement():
    from tools.weather._sources import _consensus

    sources = {
        "source_a": {
            "temp_high_c": 15.0, "temp_low_c": 8.0,
            "feels_high_c": 13.0, "feels_low_c": 6.0,
            "precip_prob_max_pct": 20, "wind_max_kmh": 10, "uv_index_max": 3,
        },
        "source_b": {
            "temp_high_c": 19.0, "temp_low_c": 9.0,
            "feels_high_c": 17.0, "feels_low_c": 7.0,
            "precip_prob_max_pct": 25, "wind_max_kmh": 12, "uv_index_max": 4,
        },
    }
    result = _consensus(sources)
    assert "disagree" in result
    flags = result["disagree"]
    assert any("temp_high_c" in f for f in flags), f"expected temp_high_c disagreement, got: {flags}"
    # Spread is 4.0
    assert any("4.0" in f for f in flags)


def test_consensus_no_disagreement_when_close():
    from tools.weather._sources import _consensus

    sources = {
        "source_a": {
            "temp_high_c": 15.0, "temp_low_c": 8.0,
            "feels_high_c": 13.0, "feels_low_c": 6.0,
            "precip_prob_max_pct": 20, "wind_max_kmh": 10, "uv_index_max": 3,
        },
        "source_b": {
            "temp_high_c": 16.0, "temp_low_c": 8.5,
            "feels_high_c": 14.0, "feels_low_c": 6.5,
            "precip_prob_max_pct": 22, "wind_max_kmh": 11, "uv_index_max": 3,
        },
    }
    result = _consensus(sources)
    assert result["disagree"] == [], f"expected no disagreement, got: {result['disagree']}"


def test_consensus_median_not_mean():
    import statistics

    from tools.weather._sources import _consensus

    sources = {
        "a": {"temp_high_c": 10.0, "temp_low_c": 5.0,
              "feels_high_c": None, "feels_low_c": None,
              "precip_prob_max_pct": None, "wind_max_kmh": None, "uv_index_max": None},
        "b": {"temp_high_c": 12.0, "temp_low_c": 6.0,
              "feels_high_c": None, "feels_low_c": None,
              "precip_prob_max_pct": None, "wind_max_kmh": None, "uv_index_max": None},
        "c": {"temp_high_c": 20.0, "temp_low_c": 10.0,
              "feels_high_c": None, "feels_low_c": None,
              "precip_prob_max_pct": None, "wind_max_kmh": None, "uv_index_max": None},
    }
    result = _consensus(sources)
    # Median of [10, 12, 20] is 12; mean would be 14
    expected_median = round(statistics.median([10.0, 12.0, 20.0]), 1)
    assert result["values"]["temp_high_c"] == expected_median
