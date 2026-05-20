"""Phase 10: calc (asteval in-process) + python_run (subprocess sandbox-exec).

calc — fast, no subprocess. Math, list comp, datetime arithmetic.
python_run — for pandas-style work. macOS-only sandbox-exec; on other OS, refuses.
"""
from __future__ import annotations

import ast
import datetime as _datetime
import logging
import os
import re
import subprocess
import sys
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any

from claude_agent_sdk import tool

from agents import config as cfg
from tools._response import ok as _ok

logger = logging.getLogger(__name__)


# Matches python dunder names (e.g. __class__, __mro__, __subclasses__).
# Anything with a leading+trailing double underscore is treated as escape
# scaffolding and rejected at parse time — see _reject_dunder_attrs.
_DUNDER_RE = re.compile(r"^__.+__$")


def _reject_dunder_attrs(expr: str) -> str | None:
    """Parse `expr` and reject any attribute whose name is a dunder.

    Returns an error message if rejected, else None.

    Defense-in-depth on top of asteval's own attribute filter. The attack
    pattern blocked here is the classic Python sandbox escape:
        (1).__class__.__mro__[-1].__subclasses__()
    which walks `object`'s subclass list to reach file/network/exec primitives.
    Legit attribute access (`.days`, `.year`, `.upper()`) still works.
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        # let asteval report the syntax error in its own voice
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and _DUNDER_RE.match(node.attr):
            return "attribute chain rejected"
    return None


def _run_asteval(expr: str, timeout_sec: float) -> tuple[Any, str | None]:
    """Returns (result, err_message_or_None). Uses asteval's safe Interpreter.

    Runs on a worker thread with ``future.result(timeout=...)`` so this is
    safe to call from non-main threads — ``signal.SIGALRM`` only works on
    the main thread and raises ValueError elsewhere, which made the previous
    implementation crash when invoked from ``asyncio.to_thread`` or any
    other worker pool.
    """
    try:
        from asteval import Interpreter
    except ImportError:
        return None, "asteval not installed"

    # Defense-in-depth dunder-chain guard. Reject attribute names like
    # __class__ / __mro__ / __subclasses__ at parse time, before asteval
    # sees the expression.
    pre_err = _reject_dunder_attrs(expr)
    if pre_err is not None:
        return None, pre_err

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

    def _eval() -> tuple[Any, str | None]:
        result = interp(expr)
        if interp.error:
            err = interp.error[0].get_error()[1]
            return None, err
        return result, None

    # ThreadPoolExecutor + future.result(timeout=...) replaces signal.SIGALRM
    # so this works from any thread, not just main. CRITICAL: do NOT use the
    # context-manager form here — `with ...:` calls shutdown(wait=True) on
    # exit, which would block the calling coroutine indefinitely waiting for
    # the runaway worker (defeating the timeout entirely). Use shutdown(wait=
    # False) and accept the orphan-thread leak. asteval has no built-in
    # runtime guard, so an `expr` like `while True: pass` will leak a thread
    # forever — bounded only by process lifetime. Worth it: blocking the
    # event loop is worse than leaking one thread.
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="asteval")
    try:
        future = pool.submit(_eval)
        try:
            return future.result(timeout=timeout_sec)
        except FuturesTimeoutError:
            return None, f"eval exceeded {timeout_sec}s"
    finally:
        pool.shutdown(wait=False)


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


def _sandbox_exec_profile(tmpdir: str, python_exec: str) -> str:
    """sandbox-exec SBPL profile for python_run.

    Locks down four classes of escape:
      - ``network*``: no sockets at all.
      - ``file-write*``: only writes inside ``tmpdir`` are allowed.
      - ``process-exec*`` / ``process-fork``: blocks ``subprocess.run(...)``
        and the like. Without this, LLM code could shell out to
        ``/usr/bin/curl`` and have curl open its own network socket —
        outside the sandboxed Python process — bypassing the network deny
        entirely. The initial ``sandbox-exec -> python`` hop is explicitly
        whitelisted so the interpreter can still start.
      - sensitive ``file-read*`` paths (ssh keys, passwd, app credential
        dirs) — selective deny rather than deny-by-default because Python's
        own startup reads a sprawling set of stdlib + frameworks paths and
        whitelisting them all is brittle. The exec+fork+network locks make
        info-disclosure-only reads hard to weaponize anyway.
    """
    # Enumerate every path the bootstrap exec might use. ``python_exec`` is
    # typically the venv shim (a thin C wrapper), which in turn execs the
    # underlying CPython binary that ``sys.base_prefix`` points to. Both
    # need to be whitelisted; on macOS sandbox-exec, ``(literal ...)`` must
    # match the literal argv[0] passed to execvp, not its realpath.
    exec_paths: list[str] = [python_exec]
    real_python = os.path.realpath(python_exec)
    if real_python not in exec_paths:
        exec_paths.append(real_python)
    base_python = os.path.join(
        sys.base_prefix, "bin", f"python{sys.version_info[0]}.{sys.version_info[1]}"
    )
    if base_python not in exec_paths:
        exec_paths.append(base_python)
    real_base = os.path.realpath(base_python)
    if real_base not in exec_paths:
        exec_paths.append(real_base)
    exec_allow = " ".join(f'(literal "{p}")' for p in exec_paths)

    return (
        "(version 1)"
        "(allow default)"
        "(deny network*)"
        # Block child exec + fork so LLM code can't shell out via subprocess
        # to a binary that opens its own network socket.
        "(deny process-fork)"
        "(deny process-exec*)"
        f"(allow process-exec* {exec_allow})"
        # Write deny + tmpdir-only allow (unchanged behavior).
        "(deny file-write*)"
        f'(allow file-write* (subpath "{tmpdir}"))'
        # Selective read deny for high-value targets. Both /etc and
        # /private/etc forms because macOS resolves /etc through a symlink.
        "(deny file-read*"
        f'  (subpath "{os.path.expanduser("~/.ssh")}")'
        f'  (subpath "{os.path.expanduser("~/.aws")}")'
        f'  (subpath "{os.path.expanduser("~/.config")}")'
        '  (literal "/etc/passwd")'
        '  (literal "/private/etc/passwd")'
        '  (literal "/etc/master.passwd")'
        '  (literal "/private/etc/master.passwd")'
        '  (subpath "/etc/ssh")'
        '  (subpath "/private/etc/ssh")'
        ')'
    )


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


ALL_TOOLS = [calc, python_run]
