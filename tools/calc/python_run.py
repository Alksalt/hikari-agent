"""``python_run`` — sandboxed Python snippet runner (macOS sandbox-exec).

For pandas-style work and anything ``calc`` can't express. Runs in a
subprocess locked down by ``sandbox-exec`` (no network, no writes
outside an ephemeral tmpdir, no child process exec/fork). On non-macOS
hosts the tool refuses — there's no equivalent local sandbox we trust
to the same degree.
"""
from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import subprocess
import sys
import tempfile
import uuid
from typing import Any

from claude_agent_sdk import tool

from agents import config as cfg
from tools._response import ok as _ok
from tools.calc._shared import REPO_ROOT, _sandbox_exec_profile

logger = logging.getLogger(__name__)


def _is_relative_to(child: pathlib.Path, parent: pathlib.Path) -> bool:
    """Return True if child is inside parent (Python 3.8-compatible)."""
    try:
        child.relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _run_sandboxed(cmd: list[str], timeout: float, cwd: str,
                   env: dict[str, str]) -> subprocess.CompletedProcess:
    """Blocking subprocess call. Runs in a thread via asyncio.to_thread."""
    return subprocess.run(
        cmd,
        capture_output=True, timeout=timeout, cwd=cwd,
        env=env,
    )


@tool(
    "python_run",
    "Run a short Python snippet in a sandbox (macOS sandbox-exec). Allowed: "
    "stdlib + the project's venv (numpy/pandas if installed). Denied: network, "
    "file writes outside an ephemeral tmpdir, subprocess execution. Timeout 5s, "
    "output capped 1MB. Returns {stdout, stderr, returncode}. "
    "Optional input_files: list of absolute paths inside data/user_photos or "
    "data/user_documents that the snippet may read.",
    {"code": str, "input_files": list},
)
async def python_run(args: dict[str, Any]) -> dict[str, Any]:
    if not bool(cfg.get("calc.python_run_enabled", True)):
        return _ok("refused: python_run disabled in config")
    if sys.platform != "darwin":
        return _ok("refused: python_run requires macOS (sandbox-exec)")
    code = args.get("code") or ""
    if not code.strip():
        return _ok("refused: empty code")
    timeout = float(cfg.get("calc.timeout_sec", 5))
    max_bytes = int(cfg.get("calc.python_run_max_output_bytes", 1_048_576))

    # Validate and resolve input_files against the allowlist.
    raw_input_files: list[str] = args.get("input_files") or []
    tmpdir = tempfile.mkdtemp(prefix=f"hikari-eval-{uuid.uuid4().hex[:8]}-")
    allowed_roots = [
        REPO_ROOT / "data" / "user_photos",
        REPO_ROOT / "data" / "user_documents",
        pathlib.Path(tmpdir),
    ]
    validated_input_files: list[str] = []
    for p in raw_input_files:
        abs_p = pathlib.Path(p).expanduser().resolve()
        if not any(_is_relative_to(abs_p, root) for root in allowed_roots):
            import shutil as _shutil
            _shutil.rmtree(tmpdir, ignore_errors=True)
            return _ok(f"refused: input_file outside allowlist: {p}")
        validated_input_files.append(str(abs_p))

    profile = _sandbox_exec_profile(tmpdir, sys.executable, tuple(validated_input_files))
    # -I (isolated) strips PYTHONPATH/PYTHONSTARTUP/user-site;
    # -S (no site) additionally blocks sitecustomize.py in the venv — without
    # -S an attacker who can drop a file into site-packages/sitecustomize.py
    # gets code execution before the sandbox profile binds.
    # Preserve DYLD_LIBRARY_PATH if set in the parent env — venv Python on
    # macOS needs it to find libpython.dylib; otherwise the subprocess fails
    # with a dyld error instead of running the user's code.
    sub_env = {"PATH": "/usr/bin:/bin"}
    if os.environ.get("DYLD_LIBRARY_PATH"):
        sub_env["DYLD_LIBRARY_PATH"] = os.environ["DYLD_LIBRARY_PATH"]
    if os.environ.get("DYLD_FALLBACK_LIBRARY_PATH"):
        sub_env["DYLD_FALLBACK_LIBRARY_PATH"] = os.environ["DYLD_FALLBACK_LIBRARY_PATH"]
    cmd = ["sandbox-exec", "-p", profile, sys.executable, "-I", "-S", "-c", code]
    import shutil
    try:
        proc = await asyncio.to_thread(_run_sandboxed, cmd, timeout, tmpdir, sub_env)
        stdout = proc.stdout.decode("utf-8", errors="replace")[:max_bytes]
        stderr = proc.stderr.decode("utf-8", errors="replace")[:max_bytes]
        return _ok(
            f"exit {proc.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}",
            data={"stdout": stdout, "stderr": stderr, "returncode": proc.returncode},
        )
    except subprocess.TimeoutExpired:
        return _ok(f"refused: timeout after {timeout}s",
                   data={"stdout": "", "stderr": "timeout", "returncode": -1})
    except FileNotFoundError:
        return _ok("refused: sandbox-exec not available")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
