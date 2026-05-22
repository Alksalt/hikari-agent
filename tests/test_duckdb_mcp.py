"""Tests for the MotherDuck DuckDB analytics MCP wiring.

Three layers:
  1. `.mcp.json` declares the duckdb server with command + args.
  2. The runtime allowlist includes `mcp__duckdb__*` so tools are
     reachable on every turn (parallel to github / notion / etc).
  3. `config/engagement.yaml:prompt_injection.wrap_patterns` includes
     a duckdb pattern so query results are wrapped via
     wrap_untrusted before the model sees them (defense-in-depth, since
     a SQL result row can contain attacker-controlled text from
     messages/facts/etc).

The third test is the integration handoff — it fails until the main
session adds `"^mcp__duckdb__"` to `wrap_patterns`. Documented in
INTEGRATION_NEEDED in the feature batch output.
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).parent.parent


def test_mcp_json_has_duckdb_entry():
    cfg = json.loads((REPO / ".mcp.json").read_text())
    assert "duckdb" in cfg["mcpServers"], "duckdb server missing from .mcp.json"
    entry = cfg["mcpServers"]["duckdb"]
    assert "command" in entry
    assert "args" in entry
    # The agent ATTACHes SQLite via sqlite_scanner; the server itself
    # boots :memory:. Don't pin the exact arg list — just sanity-check
    # the package reference and that an in-memory boot is used.
    args = entry["args"]
    assert any("mcp-server-motherduck" in a for a in args), \
        "expected mcp-server-motherduck reference in args"
    assert ":memory:" in args, "expected :memory: db-path for boot"


def test_duckdb_in_allowlist():
    from agents.runtime import allowed_tool_names
    names = allowed_tool_names()
    assert "mcp__duckdb__*" in names, \
        "duckdb wildcard missing from runtime allowlist"


def test_duckdb_in_wrap_patterns():
    """Defense-in-depth: query results may contain attacker-shaped text
    (a fact's `object` column, a message's `content`, ...). The generic
    PostToolUse wrap hook should wrap duckdb outputs with the
    HIKARI_UNTRUSTED delimiters before the model reads them.

    Phase A (step 9): wrap_patterns moved from engagement.yaml to tools.yaml.
    """
    from tools._tools_yaml import load_registry
    patterns = load_registry().wrap_patterns()
    assert any("duckdb" in p for p in patterns), \
        "no wrap_pattern covers duckdb tools in config/tools.yaml"
