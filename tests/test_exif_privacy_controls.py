"""EXIF GPS precision control and photo_locations DB helpers.

Validates:
- _apply_gps_precision: city_precision_only=true rounds to 2dp.
- _apply_gps_precision: city_precision_only=false keeps full precision.
- photo_location_insert + photo_locations_recent round-trip.
- photo_location_delete removes the row.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import patch

import pytest

from storage import db

# ---------------------------------------------------------------------------
# DB isolation
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield
    db._reset_schema_sentinel()


# ---------------------------------------------------------------------------
# _apply_gps_precision tests
# ---------------------------------------------------------------------------

def test_city_precision_rounds_to_2dp():
    """With city_precision_only=true, lat/lon are rounded to 2 decimal places."""
    from agents import config as cfg

    with patch.object(cfg, "get", side_effect=lambda k, *a: True if k == "location.exif_gps_city_precision_only" else None):
        from agents.telegram_bridge import _apply_gps_precision
        lat, lon = _apply_gps_precision(48.85827, 2.29440)
    assert lat == round(48.85827, 2)
    assert lon == round(2.29440, 2)


def test_full_precision_keeps_all_digits():
    """With city_precision_only=false, lat/lon are returned unchanged."""
    from agents import config as cfg

    with patch.object(cfg, "get", side_effect=lambda k, *a: False if k == "location.exif_gps_city_precision_only" else None):
        from agents.telegram_bridge import _apply_gps_precision
        lat, lon = _apply_gps_precision(48.85827, 2.29440)
    assert lat == 48.85827
    assert lon == 2.29440


def test_city_precision_default_is_true():
    """When config key is absent (returns None), the default is city-level rounding."""
    from agents import config as cfg

    with patch.object(cfg, "get", return_value=None):
        from agents.telegram_bridge import _apply_gps_precision
        lat, lon = _apply_gps_precision(48.85827, 2.29440)
    # Default: city-level (2dp)
    assert lat == round(48.85827, 2)
    assert lon == round(2.29440, 2)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def test_photo_location_insert_and_recent():
    row_id = db.photo_location_insert(
        lat=48.86, lon=2.29, label="Paris, France", taken_at="2026-05-01 10:00:00"
    )
    assert isinstance(row_id, int)
    assert row_id > 0

    rows = db.photo_locations_recent(limit=5)
    assert len(rows) == 1
    assert rows[0]["label"] == "Paris, France"
    assert abs(rows[0]["lat"] - 48.86) < 0.001


def test_photo_location_delete_existing():
    row_id = db.photo_location_insert(lat=35.68, lon=139.69, label="Tokyo")
    deleted = db.photo_location_delete(row_id)
    assert deleted is True
    rows = db.photo_locations_recent()
    assert len(rows) == 0


def test_photo_location_delete_nonexistent():
    deleted = db.photo_location_delete(9999)
    assert deleted is False


def test_photo_locations_recent_limit():
    for i in range(5):
        db.photo_location_insert(lat=float(i), lon=float(i), label=f"place_{i}")
    rows = db.photo_locations_recent(limit=3)
    assert len(rows) == 3
