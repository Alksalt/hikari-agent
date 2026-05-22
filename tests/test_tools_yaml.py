"""Tests for tools/_tools_yaml.py — the single-source tool registry.

Step 1 invariant: the registry is loaded and queried but NO runtime
consumers have been switched yet.  All assertions verify the new code
produces values equivalent to the old code paths so we can migrate
consumers one-by-one with confidence.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Minimal isolation — give each test a clean DB + env."""
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
    """Return a fresh (uncached) ToolRegistry from the production yaml."""
    from tools._tools_yaml import _load_yaml, DEFAULT_YAML_PATH
    return _load_yaml(DEFAULT_YAML_PATH)


# ---------------------------------------------------------------------------
# Basic loading
# ---------------------------------------------------------------------------

class TestRegistryLoads:
    def test_registry_loads_without_error(self, registry):
        assert registry is not None

    def test_validate_clean(self, registry):
        errors = registry.validate()
        assert errors == [], f"validate() returned errors: {errors}"

    def test_has_mcp_servers(self, registry):
        servers = registry.mcp_servers()
        assert len(servers) > 0

    def test_has_tool_specs(self, registry):
        specs = registry.specs()
        assert len(specs) > 0

    def test_has_subagents(self, registry):
        sub = registry._subagents_spec
        assert len(sub) > 0


# ---------------------------------------------------------------------------
# allowed_tool_names() must be a superset of the old _DEDICATED_AND_EXTERNAL_TOOLS
# (excluding utility tools, which are separately auto-discovered)
# ---------------------------------------------------------------------------

class TestAllowedToolNames:
    _EXPECTED_NON_UTILITY = [
        "Agent",
        "WebFetch",
        "WebSearch",
        "mcp__hikari_memory__recall",
        "mcp__hikari_memory__remember",
        "mcp__hikari_memory__mark_fact_invalid",
        "mcp__hikari_memory__update_core_block",
        "mcp__hikari_memory__task_create",
        "mcp__hikari_memory__task_update",
        "mcp__hikari_photo__generate_photo",
        "mcp__hikari_wiki__wiki_search",
        "mcp__hikari_wiki__wiki_read",
        "mcp__hikari_wiki__wiki_append",
        "mcp__hikari_wiki__wiki_backlinks",
        "mcp__hikari_wiki__wiki_list",
        "mcp__hikari_wiki__wiki_tree",
        "mcp__hikari_dispatch__dispatch_claude_session",
        "mcp__hikari_codex__list_codex_reports",
        "mcp__hikari_codex__read_codex_report",
        "mcp__apple_events__*",
        "mcp__apple_shortcuts__*",
        "mcp__github__*",
        "mcp__google_workspace__*",
        "mcp__notion__*",
        "mcp__playwright__*",
        "mcp__youtube_transcript__*",
        "mcp__duckdb__*",
    ]

    def test_registry_includes_all_expected(self, registry):
        names = set(registry.allowed_tool_names())
        missing = [n for n in self._EXPECTED_NON_UTILITY if n not in names]
        assert not missing, f"allowed_tool_names() missing: {missing}"

    def test_no_duplicates(self, registry):
        names = registry.allowed_tool_names()
        assert len(names) == len(set(names)), "allowed_tool_names() has duplicates"

    def test_runtime_allowed_tool_names_equivalent(self):
        """The runtime's allowed_tool_names() must still return the same
        tools as before (it appends utility-auto-discovered names on top).
        """
        from agents.runtime import allowed_tool_names
        names = allowed_tool_names()
        # These must all be present (were in the old _DEDICATED_AND_EXTERNAL_TOOLS)
        for expected in self._EXPECTED_NON_UTILITY:
            assert expected in names, f"runtime allowed_tool_names() missing {expected!r}"


# ---------------------------------------------------------------------------
# defer_gated_patterns()
# ---------------------------------------------------------------------------

class TestDeferGatedPatterns:
    def test_defer_gated_patterns_non_empty(self, registry):
        patterns = registry.defer_gated_patterns()
        assert len(patterns) > 0

    def test_dispatch_is_gated(self, registry):
        import re
        patterns = registry.defer_gated_patterns()
        assert any(
            re.fullmatch(p, "mcp__hikari_dispatch__dispatch_claude_session")
            for p in patterns
        ), "dispatch_claude_session must be in defer_gated_patterns"

    def test_gmail_sends_are_gated(self, registry):
        """Phase E: gmail_bulk_delete_messages is now gatekeeper-gated, not defer-gated.
        gmail_send_email and gmail_reply_to_email remain on the defer path."""
        import re
        patterns = registry.defer_gated_patterns()
        for tool in [
            "mcp__google_workspace__gmail_send_email",
            "mcp__google_workspace__gmail_reply_to_email",
        ]:
            assert any(re.fullmatch(p, tool) for p in patterns), (
                f"{tool} must be in defer_gated_patterns"
            )
        # gmail_bulk_delete_messages is now on the gatekeeper path.
        spec = registry._resolve("mcp__google_workspace__gmail_bulk_delete_messages")
        assert spec is not None and spec.gate == "gatekeeper", (
            "gmail_bulk_delete_messages must have gate: gatekeeper (Phase E)"
        )

    def test_notion_writes_are_gated(self, registry):
        import re
        patterns = registry.defer_gated_patterns()
        for tool in [
            "mcp__notion__API-patch-block-children",
            "mcp__notion__API-post-page",
        ]:
            assert any(re.fullmatch(p, tool) for p in patterns), (
                f"{tool} must be in defer_gated_patterns"
            )

    def test_python_run_is_gated(self, registry):
        import re
        patterns = registry.defer_gated_patterns()
        assert any(
            re.fullmatch(p, "mcp__hikari_utility__python_run")
            for p in patterns
        ), "python_run must be in defer_gated_patterns"

    def test_matches_engagement_yaml_gate_list(self):
        """Registry defer patterns must cover every entry in the old
        engagement.yaml defer_gated_tools list."""
        import re
        from agents import config as cfg
        old_patterns = cfg.get("approvals.defer_gated_tools") or []

        from tools._tools_yaml import _load_yaml, DEFAULT_YAML_PATH
        reg = _load_yaml(DEFAULT_YAML_PATH)
        new_patterns = reg.defer_gated_patterns()

        # For each old pattern, construct a representative tool name that
        # it would match and verify the new registry also matches it.
        for old_pat in old_patterns:
            # Strip anchors to get the tool name
            name = old_pat.lstrip("^").rstrip("$")
            if name.endswith(".*"):
                name = name[:-2] + "_foo"
            matched_old = bool(re.fullmatch(old_pat, name))
            matched_new = any(re.fullmatch(p, name) for p in new_patterns)
            assert matched_old == matched_new, (
                f"old pattern {old_pat!r} covers {name!r} but new registry does not"
            )


# ---------------------------------------------------------------------------
# wrap_patterns()
# ---------------------------------------------------------------------------

class TestWrapPatterns:
    def test_wrap_patterns_non_empty(self, registry):
        assert len(registry.wrap_patterns()) > 0

    def test_recall_is_wrapped(self, registry):
        import re
        pats = registry.wrap_patterns()
        assert any(re.search(p, "mcp__hikari_memory__recall") for p in pats)

    def test_google_workspace_is_wrapped(self, registry):
        import re
        pats = registry.wrap_patterns()
        assert any(re.search(p, "mcp__google_workspace__query_gmail_emails") for p in pats)

    def test_matches_engagement_yaml_wrap_patterns(self):
        """Every old engagement.yaml wrap_pattern must be present in the registry."""
        from agents import config as cfg
        old_patterns = set(cfg.get("prompt_injection.wrap_patterns") or [])

        from tools._tools_yaml import _load_yaml, DEFAULT_YAML_PATH
        reg = _load_yaml(DEFAULT_YAML_PATH)
        new_patterns = set(reg.wrap_patterns())

        missing = old_patterns - new_patterns
        assert not missing, (
            f"registry wrap_patterns() is missing these from engagement.yaml: {missing}"
        )


# ---------------------------------------------------------------------------
# untrusted_tools()
# ---------------------------------------------------------------------------

class TestUntrustedTools:
    def test_untrusted_tools_non_empty(self, registry):
        assert len(registry.untrusted_tools()) > 0

    def test_recall_is_untrusted(self, registry):
        names = registry.untrusted_tools()
        assert any("recall" in n for n in names)

    def test_matches_engagement_yaml_untrusted_tools(self):
        """Every old engagement.yaml untrusted_tools entry must be covered."""
        from agents import config as cfg
        old_entries = set(cfg.get("prompt_injection.untrusted_tools") or [])

        from tools._tools_yaml import _load_yaml, DEFAULT_YAML_PATH
        reg = _load_yaml(DEFAULT_YAML_PATH)
        new_entries = set(reg.untrusted_tools())

        missing = old_entries - new_entries
        assert not missing, (
            f"registry untrusted_tools() missing these from engagement.yaml: {missing}"
        )


# ---------------------------------------------------------------------------
# subagents()
# ---------------------------------------------------------------------------

class TestSubagents:
    def test_subagents_non_empty(self, registry):
        # _subagents_spec has the specs; subagents() requires prompt files
        assert len(registry._subagents_spec) > 0

    def test_expected_subagents_present(self, registry):
        expected = {"wiki", "drive_gmail", "notion", "research", "github"}
        actual = set(registry._subagents_spec.keys())
        missing = expected - actual
        assert not missing, f"subagents missing from registry: {missing}"

    def test_recall_and_code_dispatch_deleted(self, registry):
        actual = set(registry._subagents_spec.keys())
        assert "recall" not in actual, "recall subagent should be deleted from yaml"
        assert "code_dispatch" not in actual, "code_dispatch subagent should be deleted from yaml"

    def test_subagents_returns_agent_definitions(self, registry):
        from claude_agent_sdk import AgentDefinition
        agents = registry.subagents()
        assert len(agents) > 0
        for name, agent in agents.items():
            assert isinstance(agent, AgentDefinition), (
                f"subagent {name!r} is not AgentDefinition"
            )

    def test_subagents_shape_matches_all_agents(self, registry):
        """The registry subagents dict must have the same keys as ALL_AGENTS
        once recall + code_dispatch are removed."""
        from agents.subagents import ALL_AGENTS
        old_keys = set(ALL_AGENTS.keys()) - {"recall", "code_dispatch"}
        new_keys = set(registry._subagents_spec.keys())
        assert old_keys == new_keys, (
            f"registry subagent keys differ from ALL_AGENTS (excl deleted): "
            f"old={old_keys} new={new_keys}"
        )

    def test_prompt_files_exist(self, registry):
        for sid, spec in registry._subagents_spec.items():
            desc_path = REPO_ROOT / spec.description_path
            prompt_path = REPO_ROOT / spec.prompt_path
            assert desc_path.exists(), f"subagent {sid}: description_path not found: {desc_path}"
            assert prompt_path.exists(), f"subagent {sid}: prompt_path not found: {prompt_path}"


# ---------------------------------------------------------------------------
# MCP servers
# ---------------------------------------------------------------------------

class TestMcpServers:
    def test_bucket1_servers_have_factory(self, registry):
        for name, spec in registry.mcp_servers().items():
            if spec.bucket == 1:
                assert spec.runtime_factory, (
                    f"bucket-1 server {name!r} missing runtime_factory"
                )

    def test_bucket3_servers_have_command(self, registry):
        for name, spec in registry.mcp_servers().items():
            if spec.bucket == 3:
                assert spec.command, (
                    f"bucket-3 server {name!r} missing command"
                )

    def test_mcp_json_servers_all_in_registry(self, registry):
        """Every server in .mcp.json must be a bucket-3 server in the registry."""
        import json
        mcp_path = REPO_ROOT / ".mcp.json"
        if not mcp_path.exists():
            pytest.skip(".mcp.json not found")
        data = json.loads(mcp_path.read_text())
        mcp_servers = {k for k in data.get("mcpServers", {}) if not k.startswith("_")}
        reg_servers = {
            name for name, spec in registry.mcp_servers().items()
            if spec.bucket == 3
        }
        missing = mcp_servers - reg_servers
        assert not missing, f".mcp.json servers not in registry: {missing}"

    def test_mcp_json_has_sentinel(self):
        import json
        mcp_path = REPO_ROOT / ".mcp.json"
        if not mcp_path.exists():
            pytest.skip(".mcp.json not found")
        data = json.loads(mcp_path.read_text())
        assert "_generated_by" in data, ".mcp.json missing _generated_by sentinel"


# ---------------------------------------------------------------------------
# load_registry() caching
# ---------------------------------------------------------------------------

class TestLoadRegistry:
    def test_same_instance_returned(self):
        from tools._tools_yaml import load_registry, DEFAULT_YAML_PATH
        r1 = load_registry(DEFAULT_YAML_PATH)
        r2 = load_registry(DEFAULT_YAML_PATH)
        assert r1 is r2

    def test_different_path_different_instance(self, tmp_path):
        """A test yaml at a different path should produce a different instance."""
        from tools._tools_yaml import load_registry, DEFAULT_YAML_PATH
        r1 = load_registry(DEFAULT_YAML_PATH)
        # Create a minimal valid yaml
        mini = tmp_path / "mini.yaml"
        mini.write_text("version: 1\nmcp_servers: {}\ntools: []\nsubagents: {}\n")
        r2 = load_registry(mini)
        assert r1 is not r2
