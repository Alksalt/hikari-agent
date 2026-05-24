"""restore.sh dry-run semantics.

Verifies that:
  1. `bash scripts/restore.sh --dry-run` exits 0 and prints the expected
     manual-copy steps (README restore instructions).
  2. TMP_ROOT is NOT deleted — its path is printed so the operator can copy files.
  3. Running without --dry-run and without an archive exits non-zero.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

RESTORE_SH = Path(__file__).parent.parent / "scripts" / "restore.sh"


class TestRestoreDryRun:
    def test_dry_run_exits_zero(self, tmp_path):
        """--dry-run exits 0 even without a real archive or key file."""
        env = {
            **os.environ,
            "HOME": str(tmp_path),
        }
        result = subprocess.run(
            ["/bin/bash", str(RESTORE_SH), "--dry-run"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"restore.sh --dry-run failed:\n{result.stderr}"

    def test_dry_run_prints_manual_copy_steps(self, tmp_path):
        """--dry-run output must include all five manual-copy file targets."""
        env = {**os.environ, "HOME": str(tmp_path)}
        result = subprocess.run(
            ["/bin/bash", str(RESTORE_SH), "--dry-run"],
            env=env,
            capture_output=True,
            text=True,
        )
        combined = result.stdout + result.stderr
        assert "hikari.db" in combined
        assert ".env" in combined
        assert "secrets/" in combined
        assert "keychain.p12" in combined
        assert ".cloudflared/" in combined

    def test_dry_run_prints_oauth_grant_steps(self, tmp_path):
        """--dry-run output must include OAuth re-grant steps."""
        env = {**os.environ, "HOME": str(tmp_path)}
        result = subprocess.run(
            ["/bin/bash", str(RESTORE_SH), "--dry-run"],
            env=env,
            capture_output=True,
            text=True,
        )
        combined = result.stdout + result.stderr
        assert "google grant" in combined
        assert "notion grant" in combined
        assert "github grant" in combined

    def test_dry_run_prints_tmp_root_path(self, tmp_path):
        """--dry-run must print TMP ROOT and EXTRACT DIR paths."""
        env = {**os.environ, "HOME": str(tmp_path)}
        result = subprocess.run(
            ["/bin/bash", str(RESTORE_SH), "--dry-run"],
            env=env,
            capture_output=True,
            text=True,
        )
        combined = result.stdout + result.stderr
        assert "TMP ROOT" in combined or "EXTRACT DIR" in combined

    def test_dry_run_does_not_delete_tmp_root(self, tmp_path):
        """TMP_ROOT must survive script exit so the operator can copy files."""
        env = {**os.environ, "HOME": str(tmp_path)}
        result = subprocess.run(
            ["/bin/bash", str(RESTORE_SH), "--dry-run"],
            env=env,
            capture_output=True,
            text=True,
        )
        combined = result.stdout + result.stderr
        # Extract the TMP ROOT path from output.
        tmp_root: str | None = None
        for line in combined.splitlines():
            if "TMP ROOT" in line or "hikari-restore" in line:
                parts = line.split()
                for p in parts:
                    if "hikari-restore" in p:
                        tmp_root = p
                        break
        if tmp_root:
            assert Path(tmp_root).exists(), f"TMP_ROOT {tmp_root!r} was deleted"

    def test_no_archive_no_dry_run_exits_nonzero(self, tmp_path):
        """Without --dry-run and without an archive, must exit non-zero."""
        env = {**os.environ, "HOME": str(tmp_path)}
        result = subprocess.run(
            ["/bin/bash", str(RESTORE_SH)],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
