"""Phase 10: calc (asteval in-process) + python_run (subprocess sandbox-exec).

calc — fast, no subprocess. Math, list comp, datetime arithmetic.
python_run — for pandas-style work. macOS-only sandbox-exec; on other OS, refuses.
"""
from __future__ import annotations

import datetime as _datetime
import logging
import os
import subprocess
import sys
import tempfile
import uuid
from typing import Any

from claude_agent_sdk import tool

from agents import config as cfg

logger = logging.getLogger(__name__)


def _ok(text: str, data: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if data is not None:
        body["data"] = data
    return body


def _run_asteval(expr: str, timeout_sec: float) -> tuple[Any, str | None]:
    """Returns (result, err_message_or_None). Uses asteval's safe Interpreter."""
    import signal
    try:
        from asteval import Interpreter
    except ImportError:
        return None, "asteval not installed"

    interp = Interpreter(minimal=False)
    # Strip every builtin that would let LLM-generated code escape the
    # in-process calculator: __import__('os').system(...) was the original
    # find. Belt-and-braces — kill anything that touches the import system,
    # introspection, file/network IO, or arbitrary code execution.
    _DANGEROUS = (
        "__import__", "compile", "exec", "globals", "locals", "vars",
        "getattr", "setattr", "delattr", "hasattr", "type", "object",
        "open", "fromfile", "input", "breakpoint", "__builtins__",
        "memoryview", "bytearray", "bytes",
    )
    for _bad in _DANGEROUS:
        interp.symtable.pop(_bad, None)
    interp.symtable["datetime"] = _datetime
    interp.symtable["date"] = _datetime.date
    interp.symtable["time"] = _datetime.time
    interp.symtable["timedelta"] = _datetime.timedelta

    def _on_timeout(sig, frame):
        raise TimeoutError(f"eval exceeded {timeout_sec}s")

    old_handler = signal.signal(signal.SIGALRM, _on_timeout)
    signal.setitimer(signal.ITIMER_REAL, timeout_sec)
    try:
        result = interp(expr)
        if interp.error:
            err = interp.error[0].get_error()[1]
            return None, err
        return result, None
    except TimeoutError as e:
        return None, str(e)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)


@tool(
    "calc",
    "Evaluate a Python expression (math, list comp, date arithmetic). "
    "Safe: no imports, no file/network access, no statements. Returns the value. "
    "Examples: '17.5 * 2400 / 100', '(date(2026,5,19) - date(2026,1,1)).days', "
    "'sum(range(100))'.",
    {"expr": str},
)
async def calc(args: dict[str, Any]) -> dict[str, Any]:
    expr = (args.get("expr") or "").strip()
    if not expr:
        return _ok("refused: empty expression")
    timeout = float(cfg.get("calc.timeout_sec", 5))
    result, err = _run_asteval(expr, timeout)
    if err:
        return _ok(f"err: {err}", data={"result": None, "error": err})
    return _ok(f"{result!r}", data={"result": result})


def _sandbox_exec_profile(tmpdir: str) -> str:
    """sandbox-exec SBPL profile: deny network, allow writes only to tmpdir."""
    return (
        "(version 1)"
        "(allow default)"
        "(deny network*)"
        "(deny file-write*)"
        f"(allow file-write* (subpath \"{tmpdir}\"))"
    )


@tool(
    "python_run",
    "Run a short Python snippet in a sandbox (macOS sandbox-exec). Allowed: "
    "stdlib + the project's venv (numpy/pandas if installed). Denied: network, "
    "file writes outside an ephemeral tmpdir. Timeout 5s, output capped 1MB. "
    "Returns {stdout, stderr, returncode}.",
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
    profile = _sandbox_exec_profile(tmpdir)
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


ALL_TOOLS = [calc, python_run]
