"""Apple Notes osascript-driven tool surface.

We mock ``asyncio.create_subprocess_exec`` and assert:
  * argv shape is ``("osascript", "-e", <script>)`` — argv-only, never shell.
  * the AppleScript embeds user-supplied strings via the local ``_as_quoted``
    helper (backslash + double-quote both escaped).
  * the response shape on the happy path includes a ``data`` block.
  * non-mac platforms short-circuit without invoking subprocess.
  * a hung subprocess past ``_OSASCRIPT_TIMEOUT_SEC`` returns a clean refusal.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest


class _FakeProc:
    """Stand-in for an ``asyncio.subprocess.Process``.

    ``communicate()`` returns the canned ``(stdout, stderr)`` bytes
    after an optional ``delay`` (used to exercise the timeout path).
    """

    def __init__(
        self,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        delay: float = 0.0,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._delay = delay
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True


def _install_subprocess_mock(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
    delay: float = 0.0,
) -> dict[str, Any]:
    """Patch ``asyncio.create_subprocess_exec`` in the apple_notes module.

    Returns a dict that captures the argv each tool was invoked with so
    tests can assert on argv shape and the AppleScript body.
    """
    captured: dict[str, Any] = {"calls": []}

    async def _fake_create(*args: Any, **kwargs: Any) -> _FakeProc:
        captured["calls"].append({"argv": args, "kwargs": kwargs})
        return _FakeProc(
            stdout=stdout, stderr=stderr, returncode=returncode, delay=delay,
        )

    from tools import apple_notes
    monkeypatch.setattr(apple_notes.asyncio, "create_subprocess_exec", _fake_create)
    # Force the macOS branch regardless of host. The non-mac tests
    # patch this back to "linux" themselves.
    monkeypatch.setattr(apple_notes.sys, "platform", "darwin")
    return captured


# --- _as_quoted unit -------------------------------------------------------

def test_as_quoted_escapes_quotes_and_backslashes():
    from tools.apple_notes import _as_quoted
    # Input contains both backslash and double-quote — the two chars
    # AppleScript cares about inside a quoted string literal.
    assert _as_quoted('Test "quoted" \\backslash') == (
        '"Test \\"quoted\\" \\\\backslash"'
    )


def test_as_quoted_plain_string_unchanged_inside_quotes():
    from tools.apple_notes import _as_quoted
    assert _as_quoted("plain text") == '"plain text"'


def test_as_quoted_handles_japanese_text():
    """User is Japanese — Unicode in note titles is the common case."""
    from tools.apple_notes import _as_quoted
    result = _as_quoted("買い物リスト")
    # Result should embed the original unicode verbatim, wrapped in quotes.
    assert "買い物リスト" in result
    assert result.startswith('"')
    assert result.endswith('"')


def test_as_quoted_handles_multiline_body():
    from tools.apple_notes import _as_quoted
    result = _as_quoted("line one\nline two\nline three")
    # AppleScript embeds newlines literally — our quoter shouldn't strip them.
    assert "line one" in result
    assert "line three" in result


# --- note_create happy path ------------------------------------------------

@pytest.mark.asyncio
async def test_note_create_invokes_osascript_argv_style(monkeypatch):
    captured = _install_subprocess_mock(
        monkeypatch, stdout=b"x-coredata://abc-123\n", returncode=0,
    )
    from tools import apple_notes
    out = await apple_notes.note_create.handler(
        {"title": 'Test "quoted" \\backslash', "body": "hello\nworld"},
    )
    assert captured["calls"], "subprocess was never invoked"
    argv = captured["calls"][0]["argv"]
    # Argv-only — first element MUST be the bare binary name, second
    # MUST be -e, third is the AppleScript body. No shell metachars
    # have any meaning here.
    assert argv[0] == "osascript"
    assert argv[1] == "-e"
    script = argv[2]
    # Verify the quoter actually escaped the user's hostile title.
    assert '"Test \\"quoted\\" \\\\backslash"' in script
    # Body should appear (HTML-wrapped) inside the script.
    assert "hello" in script
    # Response shape
    assert "data" in out
    assert out["data"]["id"] == "x-coredata://abc-123"


@pytest.mark.asyncio
async def test_note_create_with_folder_scopes_script(monkeypatch):
    captured = _install_subprocess_mock(
        monkeypatch, stdout=b"x-coredata://xyz\n",
    )
    from tools import apple_notes
    await apple_notes.note_create.handler(
        {"title": "groceries", "body": "milk", "folder": "Shopping"},
    )
    script = captured["calls"][0]["argv"][2]
    assert 'tell folder "Shopping"' in script
    assert '"groceries"' in script


@pytest.mark.asyncio
async def test_note_create_refuses_empty_title(monkeypatch):
    captured = _install_subprocess_mock(monkeypatch, stdout=b"id")
    from tools import apple_notes
    out = await apple_notes.note_create.handler({"title": "", "body": "x"})
    assert "refused" in out["content"][0]["text"].lower()
    assert not captured["calls"], "should not have spawned osascript"


# --- note_search -----------------------------------------------------------

@pytest.mark.asyncio
async def test_note_search_parses_tabbed_output(monkeypatch):
    rows = (
        b"x-coredata://A\tshopping list\tMonday, January 5, 2026 at 9:00:00 AM\n"
        b"x-coredata://B\tideas\tTuesday, January 6, 2026 at 11:00:00 AM\n"
    )
    captured = _install_subprocess_mock(monkeypatch, stdout=rows)
    from tools import apple_notes
    out = await apple_notes.note_search.handler(
        {"query": 'milk "and" eggs', "limit": 10},
    )
    argv = captured["calls"][0]["argv"]
    assert argv[0] == "osascript" and argv[1] == "-e"
    script = argv[2]
    # The hostile query (containing a double quote) is properly escaped.
    assert '"milk \\"and\\" eggs"' in script
    # Two hits parsed.
    assert len(out["data"]["hits"]) == 2
    assert out["data"]["hits"][0]["title"] == "shopping list"
    assert out["data"]["hits"][1]["id"] == "x-coredata://B"


@pytest.mark.asyncio
async def test_note_search_respects_limit(monkeypatch):
    rows = b"".join(
        f"id{i}\ttitle{i}\tdate{i}\n".encode() for i in range(20)
    )
    _install_subprocess_mock(monkeypatch, stdout=rows)
    from tools import apple_notes
    out = await apple_notes.note_search.handler({"query": "x", "limit": 5})
    assert len(out["data"]["hits"]) == 5


@pytest.mark.asyncio
async def test_note_search_refuses_empty_query(monkeypatch):
    captured = _install_subprocess_mock(monkeypatch)
    from tools import apple_notes
    out = await apple_notes.note_search.handler({"query": "", "limit": 10})
    assert "refused" in out["content"][0]["text"].lower()
    assert not captured["calls"]


# --- note_read -------------------------------------------------------------

@pytest.mark.asyncio
async def test_note_read_returns_title_and_body(monkeypatch):
    captured = _install_subprocess_mock(
        monkeypatch,
        stdout=b"shopping list\n---\nmilk\neggs\nbread\n",
    )
    from tools import apple_notes
    out = await apple_notes.note_read.handler({"title_or_id": 'has"quote'})
    argv = captured["calls"][0]["argv"]
    assert argv[0] == "osascript" and argv[1] == "-e"
    script = argv[2]
    # Quoter escaped the inner double-quote.
    assert '"has\\"quote"' in script
    assert out["data"]["title"] == "shopping list"
    assert "milk" in out["data"]["body"]


@pytest.mark.asyncio
async def test_note_read_handles_no_match(monkeypatch):
    _install_subprocess_mock(monkeypatch, stdout=b"")
    from tools import apple_notes
    out = await apple_notes.note_read.handler({"title_or_id": "missing"})
    assert "no apple note" in out["content"][0]["text"].lower()


# --- non-macOS short circuit ----------------------------------------------

@pytest.mark.asyncio
async def test_non_macos_skips_subprocess(monkeypatch):
    """On linux / windows, every tool MUST refuse without spawning osascript."""
    from tools import apple_notes

    call_count = {"n": 0}

    async def _should_not_be_called(*args, **kwargs):  # noqa: ANN001
        call_count["n"] += 1
        return _FakeProc()

    monkeypatch.setattr(
        apple_notes.asyncio, "create_subprocess_exec", _should_not_be_called,
    )
    monkeypatch.setattr(apple_notes.sys, "platform", "linux")

    out_c = await apple_notes.note_create.handler({"title": "x", "body": "y"})
    out_s = await apple_notes.note_search.handler({"query": "x", "limit": 5})
    out_r = await apple_notes.note_read.handler({"title_or_id": "x"})
    for out in (out_c, out_s, out_r):
        assert "macos-only" in out["content"][0]["text"].lower()
    assert call_count["n"] == 0


# --- timeout path ---------------------------------------------------------

@pytest.mark.asyncio
async def test_timeout_returns_clean_error(monkeypatch):
    """If osascript hangs past the timeout we return a clean refusal.

    Deterministic version: instead of racing wall-clock delays (flaky on
    slow CI), patch ``asyncio.wait_for`` to raise ``TimeoutError``
    immediately. The mock subprocess is still wired up so ``kill()`` +
    cleanup paths execute. Behavior we assert is unchanged — the tool
    catches the timeout and returns a clean refusal string.
    """
    from tools import apple_notes
    _install_subprocess_mock(monkeypatch)

    async def _raise_timeout(coro, *args: Any, **kwargs: Any) -> None:
        # The real ``asyncio.wait_for`` consumes the coroutine; close ours
        # so we don't emit a "coroutine was never awaited" RuntimeWarning.
        if hasattr(coro, "close"):
            coro.close()
        raise TimeoutError("simulated osascript hang")

    monkeypatch.setattr(apple_notes.asyncio, "wait_for", _raise_timeout)

    out = await apple_notes.note_create.handler({"title": "x", "body": "y"})
    text = out["content"][0]["text"].lower()
    assert "timed out" in text or "timeout" in text


# --- osascript missing (non-mac sneaks past platform check) ---------------

@pytest.mark.asyncio
async def test_osascript_missing_returns_clean_error(monkeypatch):
    from tools import apple_notes

    async def _raise_fnf(*args, **kwargs):  # noqa: ANN001
        raise FileNotFoundError("osascript")

    monkeypatch.setattr(
        apple_notes.asyncio, "create_subprocess_exec", _raise_fnf,
    )
    monkeypatch.setattr(apple_notes.sys, "platform", "darwin")
    out = await apple_notes.note_create.handler({"title": "x", "body": "y"})
    assert "unavailable" in out["content"][0]["text"].lower()


# --- stderr non-empty surfaces as error -----------------------------------

@pytest.mark.asyncio
async def test_stderr_surfaces_as_error(monkeypatch):
    _install_subprocess_mock(
        monkeypatch,
        stdout=b"",
        stderr=b"execution error: Notes got an error: -1743",
        returncode=1,
    )
    from tools import apple_notes
    out = await apple_notes.note_create.handler({"title": "x", "body": "y"})
    assert "apple notes error" in out["content"][0]["text"].lower()


# --- ALL_TOOLS export -----------------------------------------------------

def test_all_tools_export():
    from tools.apple_notes import (
        ALL_TOOLS,
        note_create,
        note_read,
        note_search,
    )
    assert ALL_TOOLS == [note_create, note_search, note_read]
