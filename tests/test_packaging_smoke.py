"""Packaging smoke tests — repo-local import path only, no wheel build.

hikari-agent is repo-local only. These tests verify that the working-tree
import path is intact and that no stale build artifact is being used.
"""
from __future__ import annotations

import importlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_agents_runtime_importable():
    """agents.runtime must import from the repo working tree, not a wheel."""
    mod = importlib.import_module("agents.runtime")
    mod_path = Path(mod.__file__).resolve()
    assert mod_path.is_relative_to(REPO_ROOT), (
        f"agents.runtime resolved to {mod_path}, expected inside {REPO_ROOT}"
    )


def test_storage_db_importable():
    mod = importlib.import_module("storage.db")
    mod_path = Path(mod.__file__).resolve()
    assert mod_path.is_relative_to(REPO_ROOT)


def test_no_wheel_dist_info_installed():
    """No hikari-agent dist-info should exist in any site-packages directory.

    A stale installed wheel would shadow working-tree edits.
    """
    import glob as _glob
    import site

    dist_info_hits: list[str] = []
    for sp in site.getsitepackages() + [site.getusersitepackages()]:
        sp_path = Path(sp).resolve()
        # Skip the repo-local .venv managed by uv — that's expected.
        if REPO_ROOT / ".venv" in sp_path.parents or sp_path == REPO_ROOT / ".venv":
            continue
        dist_info_hits.extend(_glob.glob(str(sp_path / "hikari_agent-*.dist-info")))
        dist_info_hits.extend(_glob.glob(str(sp_path / "hikari-agent-*.dist-info")))
    assert not dist_info_hits, (
        f"Found installed hikari-agent artifacts: {dist_info_hits}\n"
        "Run `pip uninstall hikari-agent` and rely on repo-local `uv sync`."
    )
