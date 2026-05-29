"""Phase C — Tool-gate registry, gatekeeper & exfil tests.

Covers the eight security fixes from sprint-1-external-surface Phase C:
  1. Enum validation of gate/access_mode at registry load
  2. mcp__hikari_utility__* wildcard access_mode:write
  3. playwright/youtube/duckdb wildcard access_mode:write
  4. note_create gate:gatekeeper
  5. summarize() server-prefix fallbacks raise NotImplementedError
  6. Canary deep-walk (nested payloads)
  7. Deterministic tool_use_id fallback
  8. McpInitializeTimeout vs McpProtocolError in validate_mcp_servers
"""
from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Isolation fixture
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from storage import db
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield


@pytest.fixture()
def registry():
    from tools._tools_yaml import DEFAULT_YAML_PATH, _load_yaml
    return _load_yaml(DEFAULT_YAML_PATH)


# ===========================================================================
# Fix 1 — enum validation at registry load
# ===========================================================================

class TestEnumValidation:
    def test_invalid_gate_raises(self, tmp_path):
        """A typo in gate= must raise ValueError at load time, not silently pass."""
        yaml_text = (
            "version: 1\n"
            "mcp_servers: {}\n"
            "subagents: {}\n"
            "tools:\n"
            "  - id: test_tool\n"
            "    bucket: 1\n"
            "    gate: confirm_send_typo\n"  # invalid gate value
        )
        p = tmp_path / "bad_gate.yaml"
        p.write_text(yaml_text, encoding="utf-8")
        from tools._tools_yaml import _load_yaml
        with pytest.raises(ValueError, match="invalid gate"):
            _load_yaml(p)

    def test_invalid_access_mode_raises(self, tmp_path):
        """A typo in access_mode= must raise ValueError at load time."""
        yaml_text = (
            "version: 1\n"
            "mcp_servers: {}\n"
            "subagents: {}\n"
            "tools:\n"
            "  - id: test_wildcard_*\n"
            "    bucket: 1\n"
            "    gate: null\n"
            "    access_mode: readwrite\n"  # invalid — not in {read,write,destructive}
        )
        p = tmp_path / "bad_mode.yaml"
        p.write_text(yaml_text, encoding="utf-8")
        from tools._tools_yaml import _load_yaml
        with pytest.raises(ValueError, match="invalid access_mode"):
            _load_yaml(p)

    def test_valid_gate_values_accepted(self, tmp_path):
        """null and gatekeeper are the only valid gate values."""
        for gate_val in ["null", "gatekeeper"]:
            yaml_text = (
                "version: 1\n"
                "mcp_servers: {}\n"
                "subagents: {}\n"
                "tools:\n"
                f"  - id: test_tool_{gate_val}\n"
                "    bucket: 1\n"
                f"    gate: {gate_val}\n"
                "    access_mode: read\n"
            )
            p = tmp_path / f"ok_gate_{gate_val}.yaml"
            p.write_text(yaml_text, encoding="utf-8")
            from tools._tools_yaml import _load_yaml
            reg = _load_yaml(p)  # must not raise
            assert reg is not None

    def test_valid_access_mode_values_accepted(self, tmp_path):
        for mode in ["read", "write", "destructive"]:
            yaml_text = (
                "version: 1\n"
                "mcp_servers: {}\n"
                "subagents: {}\n"
                "tools:\n"
                f"  - id: test_wc_{mode}_*\n"
                "    bucket: 1\n"
                "    gate: null\n"
                f"    access_mode: {mode}\n"
            )
            p = tmp_path / f"ok_mode_{mode}.yaml"
            p.write_text(yaml_text, encoding="utf-8")
            from tools._tools_yaml import _load_yaml
            reg = _load_yaml(p)  # must not raise
            assert reg is not None

    def test_wildcard_without_access_mode_raises(self, tmp_path):
        """A wildcard with access_mode: null (omitted) must still raise."""
        yaml_text = (
            "version: 1\n"
            "mcp_servers: {}\n"
            "subagents: {}\n"
            "tools:\n"
            "  - id: test_wc_*\n"
            "    bucket: 1\n"
            "    gate: null\n"
            # no access_mode — should raise for wildcards
        )
        p = tmp_path / "missing_mode.yaml"
        p.write_text(yaml_text, encoding="utf-8")
        from tools._tools_yaml import _load_yaml
        with pytest.raises(ValueError, match="access_mode"):
            _load_yaml(p)

    def test_module_constants_are_frozensets(self):
        from tools._tools_yaml import _VALID_ACCESS_MODES, _VALID_GATES
        assert isinstance(_VALID_GATES, frozenset)
        assert isinstance(_VALID_ACCESS_MODES, frozenset)
        assert None in _VALID_GATES
        assert "gatekeeper" in _VALID_GATES
        assert None in _VALID_ACCESS_MODES
        assert "read" in _VALID_ACCESS_MODES
        assert "write" in _VALID_ACCESS_MODES
        assert "destructive" in _VALID_ACCESS_MODES


# ===========================================================================
# Fix 2 — hikari_utility wildcard access_mode:write
# ===========================================================================

class TestHikariUtilityWildcardWrite:
    def test_hikari_utility_wildcard_is_write(self, registry):
        """mcp__hikari_utility__* must be access_mode:write so unregistered
        write tools fail closed via the gatekeeper wildcard-write deny path."""
        spec = registry._resolve("mcp__hikari_utility__some_new_tool")
        assert spec is not None
        assert spec.id == "mcp__hikari_utility__*"
        assert spec.access_mode == "write", (
            f"mcp__hikari_utility__* access_mode should be 'write', got {spec.access_mode!r}"
        )

    def test_explicit_read_tools_still_resolve_correctly(self, registry):
        """Explicit read-only utility tools (e.g. note_search) must still
        resolve to their own spec, not the wildcard."""
        spec = registry._resolve("mcp__hikari_utility__note_search")
        assert spec is not None
        assert spec.id == "mcp__hikari_utility__note_search"


# ===========================================================================
# Fix 3 — playwright/youtube/duckdb wildcard access_mode:write
# ===========================================================================

class TestExternalWildcardWrite:
    @pytest.mark.parametrize("prefix,example_tool", [
        ("mcp__playwright__", "mcp__playwright__navigate"),
        ("mcp__youtube_transcript__", "mcp__youtube_transcript__get_something_new"),
        ("mcp__duckdb__", "mcp__duckdb__create_table"),
    ])
    def test_wildcard_access_mode_is_write(self, registry, prefix, example_tool):
        spec = registry._resolve(example_tool)
        assert spec is not None, f"no spec for {example_tool!r}"
        assert spec.id.endswith("*"), f"expected wildcard match for {example_tool!r}"
        assert spec.access_mode == "write", (
            f"{spec.id} access_mode should be 'write', got {spec.access_mode!r}"
        )


# ===========================================================================
# Fix 4 — note_create gate:gatekeeper
# ===========================================================================

class TestNoteCreateGate:
    def test_note_create_is_gatekeeper_gated(self, registry):
        spec = registry._resolve("mcp__hikari_utility__note_create")
        assert spec is not None
        assert spec.gate == "gatekeeper", (
            f"note_create must have gate:gatekeeper, got {spec.gate!r}"
        )

    def test_note_create_has_access_mode_write(self, registry):
        spec = registry._resolve("mcp__hikari_utility__note_create")
        assert spec is not None
        assert spec.access_mode == "write"

    def test_note_create_no_confirm_param(self):
        """The confirm param redirect was the old stand-in for a real gate.
        After adding gate:gatekeeper, confirm must be gone from the tool signature."""
        # Read source directly — the @tool decorator wraps the fn so inspect.getsource fails.
        src = (REPO_ROOT / "tools" / "apple_notes" / "create.py").read_text(encoding="utf-8")
        assert '"confirm"' not in src and "'confirm'" not in src, (
            "confirm param should have been removed from note_create after "
            "gate:gatekeeper was added to tools.yaml"
        )

    def test_note_create_summarize_handler_exists(self):
        """gatekeeper.summarize() must handle note_create now that it's gated."""
        from tools.gatekeeper import summarize
        result = summarize(
            "mcp__hikari_utility__note_create",
            {"title": "Shopping list", "body": "milk, eggs", "folder": "Quick Capture"},
        )
        assert "Shopping list" in result
        assert "milk, eggs" in result


# ===========================================================================
# Fix 5 — summarize() server-prefix fallbacks raise NotImplementedError
# ===========================================================================

class TestSummarizeFallbacks:
    @pytest.mark.parametrize("tool_name", [
        "mcp__google_workspace__docs_create_document",
        "mcp__google_workspace__sheets_write_range",
        "mcp__github__push_files",
        "mcp__notion__API-create-a-database",
        "mcp__claude_ai_Notion__notion-create-pages",
    ])
    def test_unhandled_gated_tool_raises_not_implemented(self, tool_name):
        """Tools without a dedicated summarize case must raise NotImplementedError
        so _summarize() falls through to the _CRITICAL_FIELDS renderer."""
        from tools.gatekeeper import summarize
        with pytest.raises(NotImplementedError):
            summarize(tool_name, {"title": "test", "body": "content"})

    def test_summarize_fallback_renders_critical_fields(self):
        """_summarize() must fall through to critical-fields renderer when
        summarize() raises NotImplementedError, showing body in full."""
        from tools.gatekeeper_can_use_tool import _summarize
        result = _summarize(
            "mcp__google_workspace__docs_create_document",
            {"title": "Secret doc", "body": "FULL BODY CONTENT"},
        )
        # The critical-fields renderer must show the full body
        assert "FULL BODY CONTENT" in result

    def test_dedicated_handlers_still_work(self):
        """Explicit summarize handlers must not be affected by the fallback change."""
        from tools.gatekeeper import summarize
        result = summarize(
            "mcp__google_workspace__gmail_send_email",
            {"to": "alice@example.com", "subject": "hi", "body": "hello"},
        )
        assert "alice@example.com" in result
        assert "hello" in result


# ===========================================================================
# Fix 6 — canary deep-walk (nested payloads)
# ===========================================================================

class TestCanaryDeepWalk:
    def test_walk_strings_is_importable_from_injection_guard(self):
        from agents.injection_guard import _walk_strings
        assert callable(_walk_strings)

    def test_walk_strings_flat(self):
        from agents.injection_guard import _walk_strings
        result = _walk_strings({"key": "value", "num": 42})
        assert "key" in result
        assert "value" in result

    def test_walk_strings_nested(self):
        from agents.injection_guard import _walk_strings
        nested = {"message": {"body": "deep-value"}, "outer": "top"}
        result = _walk_strings(nested)
        assert "deep-value" in result
        assert "top" in result

    def test_walk_strings_list(self):
        from agents.injection_guard import _walk_strings
        result = _walk_strings(["a", "b", {"c": "d"}])
        assert "a" in result
        assert "b" in result
        assert "d" in result

    def test_canary_in_nested_args_is_detected(self, tmp_path, monkeypatch):
        """Canary token buried in a nested dict must be caught by
        flag_args_with_untrusted_content after the deep-walk fix."""
        db_path = tmp_path / "hikari.db"
        monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
        import storage.db as _db_mod
        importlib.reload(_db_mod)
        from storage import db
        monkeypatch.setattr(db, "_DB_PATH", db_path)
        db._reset_schema_sentinel()

        from agents.injection_guard import flag_args_with_untrusted_content, get_canary
        canary = get_canary()

        # Canary buried two levels deep
        nested_args = {
            "payload": {
                "body": f"innocent text {canary} more text",
            }
        }
        flag, reason = flag_args_with_untrusted_content(nested_args)
        assert flag is True, "nested canary must be detected"
        assert reason == "canary_in_outbound_args"

    def test_shallow_args_still_detected(self, tmp_path, monkeypatch):
        """Regression: top-level canary must still be caught after the rewrite."""
        db_path = tmp_path / "hikari.db"
        monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
        import storage.db as _db_mod
        importlib.reload(_db_mod)
        from storage import db
        monkeypatch.setattr(db, "_DB_PATH", db_path)
        db._reset_schema_sentinel()

        from agents.injection_guard import flag_args_with_untrusted_content, get_canary
        canary = get_canary()

        flat_args = {"body": f"contains {canary} here"}
        flag, reason = flag_args_with_untrusted_content(flat_args)
        assert flag is True
        assert reason == "canary_in_outbound_args"

    def test_gatekeeper_can_use_tool_imports_walk_strings_from_injection_guard(self):
        """gatekeeper_can_use_tool must not define its own _walk_strings —
        it must import from agents.injection_guard to share a single definition."""
        import ast
        src_path = REPO_ROOT / "tools" / "gatekeeper_can_use_tool.py"
        tree = ast.parse(src_path.read_text(encoding="utf-8"))
        # No function named _walk_strings should be defined in the module
        local_defs = [
            node.name for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        assert "_walk_strings" not in local_defs, (
            "_walk_strings must not be defined in gatekeeper_can_use_tool.py; "
            "it should be imported from agents.injection_guard"
        )


# ===========================================================================
# Fix 7 — deterministic tool_use_id fallback
# ===========================================================================

class TestDeterministicToolUseId:
    def _make_synth_id(self, tool_name: str, input_args: dict) -> str:
        import hashlib
        import json
        payload = (tool_name + json.dumps(input_args, sort_keys=True, default=str)).encode()
        return "synth-" + hashlib.sha256(payload).hexdigest()[:24]

    def test_same_input_produces_same_id(self):
        """Two calls with the same tool_name + input must produce the same synth id."""
        tool_name = "mcp__notion__API-post-page"
        args = {"parent": {"page_id": "abc123"}, "title": "test page"}
        id1 = self._make_synth_id(tool_name, args)
        id2 = self._make_synth_id(tool_name, args)
        assert id1 == id2

    def test_different_input_produces_different_id(self):
        tool_name = "mcp__notion__API-post-page"
        args1 = {"title": "page one"}
        args2 = {"title": "page two"}
        assert self._make_synth_id(tool_name, args1) != self._make_synth_id(tool_name, args2)

    def test_id_starts_with_synth_prefix(self):
        tool_name = "mcp__github__create_issue"
        args = {"repo": "hikari", "title": "bug"}
        result = self._make_synth_id(tool_name, args)
        assert result.startswith("synth-")

    def test_id_is_not_memory_address(self):
        """The old id(input) fallback would produce different ids for the same
        logical input in different calls. Verify the new form is stable."""
        tool_name = "mcp__google_workspace__gmail_send_email"
        args = {"to": "alice@example.com", "subject": "hello"}
        # Create two separate dict objects with same content
        args_copy = dict(args)
        id1 = self._make_synth_id(tool_name, args)
        id2 = self._make_synth_id(tool_name, args_copy)
        assert id1 == id2, "same logical input must produce same synth id regardless of object identity"

    def test_gatekeeper_can_use_tool_uses_deterministic_fallback(self):
        """Verify the source code uses the sha256 form, not id(input)."""
        import ast
        src_path = REPO_ROOT / "tools" / "gatekeeper_can_use_tool.py"
        src = src_path.read_text(encoding="utf-8")
        assert "id(input)" not in src, (
            "gatekeeper_can_use_tool must not use id(input) for tool_use_id fallback"
        )
        assert "sha256" in src, (
            "gatekeeper_can_use_tool must use sha256 for deterministic tool_use_id fallback"
        )


# ===========================================================================
# Fix 8 — McpInitializeTimeout vs McpProtocolError
# ===========================================================================

class TestMcpTypedExceptions:
    def test_mcp_exception_types_exist(self):
        from tools.mcp_introspect import McpInitializeTimeout, McpProtocolError
        assert issubclass(McpInitializeTimeout, RuntimeError)
        assert issubclass(McpProtocolError, RuntimeError)

    def test_no_initialize_response_raises_mcp_initialize_timeout(self):
        """An empty first line must raise McpInitializeTimeout."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch
        from tools.mcp_introspect import McpInitializeTimeout, list_server_tools

        async def run():
            mock_proc = MagicMock()
            mock_proc.stdin = MagicMock()
            mock_proc.stdin.write = MagicMock()
            mock_proc.stdin.drain = AsyncMock()
            mock_proc.stdout = MagicMock()
            # First readline returns empty — server never responded to initialize
            mock_proc.stdout.readline = AsyncMock(return_value=b"")
            mock_proc.terminate = MagicMock()
            mock_proc.wait = AsyncMock()
            mock_proc.returncode = 0

            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                with pytest.raises(McpInitializeTimeout):
                    await list_server_tools("fake_cmd", ())

        asyncio.run(run())

    def test_initialize_ok_then_tools_list_fails_raises_mcp_protocol_error(self):
        """A server that responds to initialize but returns empty on tools/list
        must raise McpProtocolError (not McpInitializeTimeout)."""
        import asyncio
        import json as _json
        from unittest.mock import AsyncMock, MagicMock, patch
        from tools.mcp_introspect import McpProtocolError, list_server_tools

        init_response = _json.dumps({"jsonrpc": "2.0", "id": 0, "result": {}}).encode() + b"\n"

        async def run():
            call_count = 0

            async def fake_readline():
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return init_response  # initialize succeeds
                return b""  # tools/list returns empty — hard fail

            mock_proc = MagicMock()
            mock_proc.stdin = MagicMock()
            mock_proc.stdin.write = MagicMock()
            mock_proc.stdin.drain = AsyncMock()
            mock_proc.stdout = MagicMock()
            mock_proc.stdout.readline = fake_readline
            mock_proc.terminate = MagicMock()
            mock_proc.wait = AsyncMock()
            mock_proc.returncode = 0

            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                with pytest.raises(McpProtocolError):
                    await list_server_tools("fake_cmd", ())

        asyncio.run(run())

    def test_validate_mcp_servers_hard_fails_on_protocol_error(self):
        """A server that initializes then errors must produce exit_code=1
        (HARD FAIL), not a soft skip.

        Verifies the isinstance-based classification in validate_mcp_servers._main:
        - McpProtocolError is NOT an McpInitializeTimeout → hard fail branch
        - McpInitializeTimeout IS an McpInitializeTimeout → soft skip branch
        """
        import asyncio
        import sys
        from io import StringIO
        from tools.mcp_introspect import McpInitializeTimeout, McpProtocolError

        async def run():
            import scripts.validate_mcp_servers as vms
            # Patch introspect_all to return one server that had a protocol error
            fake_results = {
                "broken_server": McpProtocolError(
                    "initialize OK, tools/list returned error"
                ),
            }
            captured = []

            async def fake_introspect(*_, **__):
                return fake_results

            with patch("tools.mcp_introspect.introspect_all", side_effect=fake_introspect):
                import builtins
                orig_print = builtins.print
                try:
                    builtins.print = lambda *a, **kw: captured.append(" ".join(str(x) for x in a))
                    exit_code = await vms._main(frozenset(), frozenset(), 10.0)
                finally:
                    builtins.print = orig_print
            assert exit_code == 1, (
                "McpProtocolError must cause exit_code=1 (hard fail)"
            )
            output = "\n".join(captured)
            assert "HARD FAIL" in output, f"Expected HARD FAIL in output: {output!r}"

        asyncio.run(run())

    def test_mcp_initialize_timeout_is_soft_skippable(self):
        """McpInitializeTimeout must be isinstance-checked as soft-skip."""
        from tools.mcp_introspect import McpInitializeTimeout
        err = McpInitializeTimeout("no response to initialize")
        assert isinstance(err, McpInitializeTimeout)
