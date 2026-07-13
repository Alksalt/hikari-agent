"""Owner-only filesystem permissions for the Hikari SQLite database."""
from __future__ import annotations

import os
import stat

import pytest

from storage import db


@pytest.fixture()
def isolated_path(tmp_path, monkeypatch):
    path = tmp_path / "hikari.db"
    monkeypatch.setattr(db, "_DB_PATH", path)
    db._reset_schema_sentinel()
    yield path
    db._reset_schema_sentinel()


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_fresh_database_is_created_owner_only(isolated_path):
    assert not isolated_path.exists()
    db.get_session_id()
    assert _mode(isolated_path) == 0o600


def test_existing_database_mode_is_repaired_before_open(isolated_path):
    isolated_path.touch(mode=0o644)
    os.chmod(isolated_path, 0o644)
    assert _mode(isolated_path) == 0o644

    db.get_session_id()

    assert _mode(isolated_path) == 0o600


def test_wal_and_shm_sidecars_are_repaired_owner_only(isolated_path):
    db.runtime_set("permission_probe", "ok")
    sidecars = [
        path for path in (
            isolated_path.with_name(isolated_path.name + "-wal"),
            isolated_path.with_name(isolated_path.name + "-shm"),
        ) if path.exists()
    ]
    assert sidecars
    for path in sidecars:
        os.chmod(path, 0o644)

    db._repair_db_sidecar_permissions(isolated_path)

    assert all(_mode(path) == 0o600 for path in sidecars)


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform lacks O_NOFOLLOW")
def test_database_symlink_is_rejected(tmp_path):
    target = tmp_path / "target.db"
    target.touch(mode=0o600)
    link = tmp_path / "hikari.db"
    link.symlink_to(target)

    with pytest.raises(OSError):
        db._ensure_db_file_owner_only(link)
