"""``python_run`` — sandboxed Python snippet runner (macOS sandbox-exec).

For pandas-style work and anything ``calc`` can't express. Runs in a
subprocess locked down by ``sandbox-exec`` (no network, no writes
outside an ephemeral tmpdir, no child process exec/fork). On non-macOS
hosts the tool refuses — there's no equivalent local sandbox we trust
to the same degree.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import uuid
from typing import Any

from claude_agent_sdk import tool

from agents import config as cfg
from tools._response import ok as _ok
from tools.calc._shared import _sandbox_exec_profile

logger = logging.getLogger(__name__)


@tool(
    "python_run",
    "Run a short Python snippet in a sandbox (macOS sandbox-exec). Allowed: "
    "stdlib + the project's venv (numpy/pandas if installed). Denied: network, "
    "file writes outside an ephemeral tmpdir, subprocess execution. Timeout 5s, "
    "output capped 1MB. Returns {stdout, stderr, returncode}.",
    {"code": str},
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
    tmpdir = tempfile.mkdtemp(prefix=f"hikari-eval-{uuid.uuid4().hex[:8]}-")
    profile = _sandbox_exec_profile(tmpdir, sys.executable)
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
    try:
        proc = subprocess.run(
            ["sandbox-exec", "-p", profile, sys.executable, "-I", "-S", "-c", code],
            capture_output=True, timeout=timeout, cwd=tmpdir,
            env=sub_env,
        )
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
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
