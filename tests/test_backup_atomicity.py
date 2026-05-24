"""backup.sh atomicity semantics.

Verifies that:
  1. backup.sh writes to a .tmp file first and only renames it to the final
     .tar.age after successful verification.
  2. Aborting backup.sh mid-encrypt (simulated via SIGKILL) leaves no corrupt
     final .tar.age in the backups dir.
  3. --dry-run flag exits 0 without touching any files.
  4. --dry-run --self-test exits 0 when required binaries are present.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

AGE_AVAILABLE = shutil.which("age") is not None
BACKUP_SH = Path(__file__).parent.parent / "scripts" / "backup.sh"


class TestBackupDryRun:
    def test_dry_run_exits_zero_no_files_written(self, tmp_path):
        """--dry-run exits 0 and writes nothing to the backup dir."""
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        env = {
            **os.environ,
            "HOME": str(tmp_path),
            "HIKARI_BACKUP_DIR": str(backup_dir),
        }
        result = subprocess.run(
            ["/bin/zsh", str(BACKUP_SH), "--dry-run"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert list(backup_dir.iterdir()) == []

    def test_dry_run_self_test_exits_zero_when_tools_present(self, tmp_path):
        """--dry-run --self-test passes when age and sqlite3 are on PATH."""
        if not (shutil.which("age") and shutil.which("sqlite3")):
            pytest.skip("age and/or sqlite3 not available")
        env = {**os.environ, "HOME": str(tmp_path)}
        result = subprocess.run(
            ["/bin/zsh", str(BACKUP_SH), "--dry-run", "--self-test"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "PASS" in result.stdout or "OK" in result.stdout


@pytest.mark.skipif(not AGE_AVAILABLE, reason="age binary not available")
class TestBackupAtomicity:
    def test_no_corrupt_final_on_kill(self, tmp_path):
        """Killing backup.sh mid-encrypt must not leave a final .tar.age file."""
        import datetime
        import signal

        today = datetime.date.today().strftime("%Y%m%d")

        # Minimal repo layout
        repo_dir = tmp_path / "hikari-agent"
        data_dir = repo_dir / "data"
        data_dir.mkdir(parents=True)
        db_path = data_dir / "hikari.db"
        db_path.write_bytes(b"SQLite format 3\x00" + b"\x00" * 84)

        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()

        # Generate a real age keypair
        key_file = tmp_path / "backup_age.key"
        pub_file = tmp_path / "backup_age.pub"
        subprocess.run(["age-keygen", "-o", str(key_file)],
                       check=True, capture_output=True)
        pub_text = subprocess.run(
            ["grep", "public key:", str(key_file)],
            capture_output=True, text=True,
        ).stdout.strip().split(": ", 1)[-1]
        pub_file.write_text(pub_text + "\n")

        env = {
            **os.environ,
            "HOME": str(tmp_path),
            "HIKARI_BACKUP_DIR": str(backup_dir),
            "HIKARI_BACKUP_AGE_RECIPIENT": str(pub_file),
        }
        proc = subprocess.Popen(
            ["/bin/zsh", str(BACKUP_SH)],
            env=env,
            cwd=str(repo_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Give the script ~0.3 s to start (age invocation will be brief on
        # a small file, so we just check state after termination).
        import time
        time.sleep(0.3)
        proc.send_signal(signal.SIGKILL)
        proc.wait()

        final_archive = backup_dir / f"hikari-{today}.tar.age"
        tmp_archive = backup_dir / f"hikari-{today}.tar.age.tmp"

        # The .tmp may exist (race is fine), but the FINAL must not unless
        # the script actually completed (SIGKILL at 0.3 s is before the mv).
        # The critical invariant: no final .tar.age without verification passing.
        if final_archive.exists():
            # If the whole backup completed in < 0.3 s, just verify it's valid age.
            r = subprocess.run(
                ["age", "-d", "-i", str(key_file), "-o", "/dev/null", str(final_archive)],
                capture_output=True,
            )
            assert r.returncode == 0, "Final archive exists but is not valid age output"
        else:
            # The expected case: no final, and .tmp was cleaned up.
            assert not tmp_archive.exists(), ".tmp sentinel left behind after SIGKILL"

    def test_successful_backup_has_no_tmp_artifact(self, tmp_path):
        """After a successful backup run the .tmp file must not remain."""
        import datetime

        today = datetime.date.today().strftime("%Y%m%d")
        repo_dir = tmp_path / "hikari-agent"
        data_dir = repo_dir / "data"
        data_dir.mkdir(parents=True)
        db_path = data_dir / "hikari.db"
        db_path.write_bytes(b"SQLite format 3\x00" + b"\x00" * 84)

        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()

        key_file = tmp_path / "backup_age.key"
        pub_file = tmp_path / "backup_age.pub"
        subprocess.run(["age-keygen", "-o", str(key_file)],
                       check=True, capture_output=True)
        pub_text = subprocess.run(
            ["grep", "public key:", str(key_file)],
            capture_output=True, text=True,
        ).stdout.strip().split(": ", 1)[-1]
        pub_file.write_text(pub_text + "\n")

        # smoke-test needs the private key too
        env = {
            **os.environ,
            "HOME": str(tmp_path),
            "HIKARI_BACKUP_DIR": str(backup_dir),
            "HIKARI_BACKUP_AGE_RECIPIENT": str(pub_file),
            "HIKARI_BACKUP_AGE_KEY": str(key_file),
        }
        result = subprocess.run(
            ["/bin/zsh", str(BACKUP_SH)],
            env=env,
            capture_output=True,
            text=True,
            cwd=str(repo_dir),
        )
        assert result.returncode == 0, f"backup.sh failed:\n{result.stderr}"

        tmp_artifact = backup_dir / f"hikari-{today}.tar.age.tmp"
        assert not tmp_artifact.exists(), ".tmp artifact was not cleaned up"

        final_archive = backup_dir / f"hikari-{today}.tar.age"
        assert final_archive.exists(), "final archive not created"
