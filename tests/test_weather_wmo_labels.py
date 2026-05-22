"""Tests for WMO code → label mapping."""
from __future__ import annotations


def test_wmo_known_codes():
    from tools.weather._shared import _WMO, wmo_label

    for code, expected_label in _WMO.items():
        result = wmo_label(code)
        assert result == expected_label, f"code {code}: expected {expected_label!r}, got {result!r}"


def test_wmo_unknown_code_fallback():
    from tools.weather._shared import wmo_label

    assert wmo_label(199) == "code 199"


def test_wmo_none_fallback():
    from tools.weather._shared import wmo_label

    result = wmo_label(None)
    assert "None" in result or result.startswith("code "), f"expected fallback for None, got {result!r}"


def test_wmo_clear_is_zero():
    from tools.weather._shared import wmo_label

    assert wmo_label(0) == "clear"


def test_wmo_thunderstorm():
    from tools.weather._shared import wmo_label

    assert wmo_label(95) == "thunderstorm"
