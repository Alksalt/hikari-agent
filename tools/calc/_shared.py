"""Shared helpers for the calc tools.

``calc`` (in-process asteval) and ``python_run`` (subprocess sandbox-exec)
sit side by side because they share the same defensive vocabulary:
dunder-chain rejection, the dangerous-builtins strip list, and the
macOS sandbox-exec profile. Two distinct attack surfaces — in-process
vs. subprocess — but the same "what could a hostile expression touch"
threat model, so the constants and helpers belong together.

Heavy imports (``asteval``) are deferred to call sites so the module
remains cheap to import at registry-discovery time.
"""
from __future__ import annotations

import ast
import datetime as _datetime
import os
import pathlib
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


# Matches python dunder names (e.g. __class__, __mro__, __subclasses__).
# Anything with a leading+trailing double underscore is treated as escape
# scaffolding and rejected at parse time — see _reject_dunder_attrs.
_DUNDER_RE = re.compile(r"^__.+__$")


# Builtins stripped from asteval's symtable. ``Interpreter(minimal=False)``
# exposes ``__import__`` (the original find) plus the rest of the standard
# escape-hatch family. Belt-and-braces — kill anything that touches the
# import system, introspection, file/network IO, or arbitrary code
# execution.
_DANGEROUS: tuple[str, ...] = (
    "__import__", "compile", "exec", "globals", "locals", "vars",
    "getattr", "setattr", "delattr", "hasattr", "type", "object",
    "open", "fromfile", "input", "breakpoint", "__builtins__",
    "memoryview", "bytearray", "bytes",
)


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


def _sandbox_exec_profile(
    tmpdir: str,
    python_exec: str,
    input_files: tuple[str, ...] = (),
) -> str:
    """sandbox-exec SBPL profile for python_run — deny-default edition.

    Security model (SBPL rules are last-match-wins):

      1. ``(deny default)`` — block everything not explicitly allowed.
      2. ``(deny network*)`` — belt-and-suspenders network block (no sockets).
      3. ``(deny process-fork)`` + ``(deny process-exec*)`` — block child
         spawn. Re-allow added next so the initial sandbox-exec → python hop
         can proceed.
      4. ``(allow process-exec* (subpath home))`` — Python itself lives inside
         the user home on uv-managed installs, so we must allow exec from home.
         Combined with the fork+network deny, an attacker can't spawn curl or
         sh from user code even though exec is permitted for the interpreter.
      5. ``(allow file-read*)`` — Python's startup reads a sprawling set of
         system dylibs, frameworks, and locale data. The only practical way to
         cover all of that without maintaining a brittle per-machine whitelist
         is a broad read allow narrowed by targeted denies (step 6).
      6. Targeted ``(deny file-read* ...)`` for high-value secrets inside the
         home directory:  ``~/.ssh``, ``~/.aws``, ``~/.gnupg``, ``~/.config``,
         ``~/Library``, ``~/.env``, the repo's ``secrets/`` directory, and
         the standard ``/etc/passwd`` / ``/private/etc/passwd`` pair.
      7. ``(allow file-write* (subpath tmpdir))`` — the only writable location
         (both the ``/var/folders/...`` form and its ``/private/var/...``
         realpath, since macOS creates tmpfiles under the symlinked path).

    Why ``(allow file-read*)`` instead of explicit subpaths?
    On this machine's Python stack (uv + cpython-3.12, aarch64, macOS 15)
    the deny-default + explicit-subpath approach causes SIGABRT during
    Python startup — the interpreter needs to read paths that vary per
    machine (dyld caches, framework versions, locale data) and are
    impractical to enumerate statically. Broad read + targeted secret-deny
    achieves the actual security goal (protect secrets) without fragility.

    Why not ``(deny file-read* (subpath home))``?
    The exec itself needs to read the interpreter binary (which lives inside
    home on uv installs). SBPL's ``deny file-read*`` blocks the read that
    ``execvp`` performs before the process-exec allow takes effect, so a
    home-wide deny prevents the interpreter from starting at all.
    """
    home = str(pathlib.Path.home())
    real_tmpdir = os.path.realpath(tmpdir)

    # Per-input-file re-allows: both literal and realpath so SBPL matches
    # regardless of whether the caller used the /var or /private/var form.
    input_file_rules: list[str] = []
    for p in input_files:
        input_file_rules.append(f'(allow file-read* (literal "{p}"))')
        real_p = os.path.realpath(p)
        if real_p != p:
            input_file_rules.append(f'(allow file-read* (literal "{real_p}"))')
    input_file_block = "".join(input_file_rules)

    # Tmpdir: the /var/folders/... form is returned by tempfile.mkdtemp();
    # the /private/var/... realpath may differ and both need write + read.
    tmpdir_allows = (
        f'(allow file-read* (subpath "{tmpdir}"))'
        f'(allow file-write* (subpath "{tmpdir}"))'
    )
    if real_tmpdir != tmpdir:
        tmpdir_allows += (
            f'(allow file-read* (subpath "{real_tmpdir}"))'
            f'(allow file-write* (subpath "{real_tmpdir}"))'
        )

    return (
        "(version 1)"
        # 1. Deny everything by default.
        "(deny default)"
        # 2. Belt-and-suspenders network block.
        "(deny network*)"
        # 3. Block child processes.
        "(deny process-fork)"
        "(deny process-exec*)"
        # 4. Allow exec only for known interpreter/toolchain paths.
        #    Narrowed from (subpath home) to avoid allowing arbitrary home binaries.
        #    If Python startup SIGABRTs, add the offending path here incrementally.
        f'(allow process-exec* (subpath "{os.path.join(home, ".local/share/uv")}"))'
        f'(allow process-exec* (subpath "{os.path.join(home, ".pyenv")}"))'
        f'(allow process-exec* (subpath "{sys.base_prefix}"))'
        f'(allow process-exec* (subpath "{sys.prefix}"))'
        # 5. Broad file-read allow (narrowed by targeted denies below).
        "(allow file-read*)"
        # 6. Targeted secret denies — these MUST come after (allow file-read*)
        #    because SBPL is last-match-wins.
        # --- Home-dir credential/config dirs ---
        f'(deny file-read* (subpath "{os.path.join(home, ".ssh")}"))'
        f'(deny file-read* (subpath "{os.path.join(home, ".aws")}"))'
        f'(deny file-read* (subpath "{os.path.join(home, ".gnupg")}"))'
        f'(deny file-read* (subpath "{os.path.join(home, ".config")}"))'
        f'(deny file-read* (subpath "{os.path.join(home, "Library")}"))'
        f'(deny file-read* (literal "{os.path.join(home, ".env")}"))'
        # --- Home-dir credential dotfiles ---
        f'(deny file-read* (literal "{os.path.join(home, ".netrc")}"))'
        f'(deny file-read* (literal "{os.path.join(home, ".npmrc")}"))'
        f'(deny file-read* (literal "{os.path.join(home, ".pypirc")}"))'
        f'(deny file-read* (literal "{os.path.join(home, ".gitconfig")}"))'
        f'(deny file-read* (literal "{os.path.join(home, ".git-credentials")}"))'
        f'(deny file-read* (subpath "{os.path.join(home, ".docker")}"))'
        f'(deny file-read* (subpath "{os.path.join(home, ".kube")}"))'
        f'(deny file-read* (subpath "{os.path.join(home, ".cargo")}"))'
        # --- Home-dir shell/tool history files (pasted secrets) ---
        f'(deny file-read* (literal "{os.path.join(home, ".zsh_history")}"))'
        f'(deny file-read* (literal "{os.path.join(home, ".bash_history")}"))'
        f'(deny file-read* (literal "{os.path.join(home, ".python_history")}"))'
        f'(deny file-read* (literal "{os.path.join(home, ".node_repl_history")}"))'
        f'(deny file-read* (literal "{os.path.join(home, ".sqlite_history")}"))'
        f'(deny file-read* (literal "{os.path.join(home, ".psql_history")}"))'
        f'(deny file-read* (literal "{os.path.join(home, ".lesshst")}"))'
        f'(deny file-read* (literal "{os.path.join(home, ".viminfo")}"))'
        # --- Home-dir user-content directories ---
        f'(deny file-read* (subpath "{os.path.join(home, "Documents")}"))'
        f'(deny file-read* (subpath "{os.path.join(home, "Desktop")}"))'
        f'(deny file-read* (subpath "{os.path.join(home, "Downloads")}"))'
        # --- Repo-internal secrets ---
        f'(deny file-read* (literal "{REPO_ROOT}/.env"))'
        f'(deny file-read* (literal "{REPO_ROOT}/.env.local"))'
        f'(deny file-read* (literal "{REPO_ROOT}/.mcp.json"))'
        f'(deny file-read* (subpath "{REPO_ROOT}/.git"))'
        f'(deny file-read* (subpath "{REPO_ROOT}/data"))'
        f'(deny file-read* (subpath "{REPO_ROOT}/secrets"))'
        # --- System credential / shared ---
        '(deny file-read* (literal "/etc/passwd") (literal "/private/etc/passwd"))'
        '(deny file-read* (literal "/etc/master.passwd")'
        ' (literal "/private/etc/master.passwd"))'
        '(deny file-read* (subpath "/etc/ssh") (subpath "/private/etc/ssh"))'
        '(deny file-read* (subpath "/Volumes"))'
        '(deny file-read* (subpath "/var/db"))'
        '(deny file-read* (subpath "/private/var/db"))'
        '(deny file-read* (subpath "/tmp"))'
        '(deny file-read* (subpath "/private/tmp"))'
        # 7. Re-allow opt-in data subpaths (last-match-wins overrides the
        #    broad data/ deny above). Tmpdir is the only writable location;
        #    input_files are re-allowed after the denies.
        # Note: tmpdir lives under /private/var/folders/, NOT /private/tmp,
        #       so the (deny /private/tmp) above does NOT block it.
        f'(allow file-read* (subpath "{REPO_ROOT}/data/user_photos"))'
        f'(allow file-read* (subpath "{REPO_ROOT}/data/user_documents"))'
        + tmpdir_allows
        + input_file_block
    )
