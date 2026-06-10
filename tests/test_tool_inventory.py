"""Tool inventory regression net.

The May 20 'allowlist' hallucination happened because Hikari had no
grounded source for her tool surface. ``tool_inventory.format_for_injection``
fills that gap. These tests pin the block format so it doesn't quietly
drift away from the structure the persona was trained against.
"""
from __future__ import annotations

import pytest

from agents import tool_inventory


def test_block_starts_with_correct_header():
    block = tool_inventory.format_for_injection()
    assert block.startswith("# tools available")


def test_in_process_tools_are_listed(monkeypatch):
    block = tool_inventory.format_for_injection()
    # Sanity: a handful of in-process tools known to be in
    # _BASE_ALLOWED_TOOLS appear by name.
    for needle in (
        "reminder_create", "recall", "weather_fetch", "calc", "wiki_search",
    ):
        assert needle in block, f"expected {needle!r} in inventory block"


def test_subagent_names_appear():
    block = tool_inventory.format_for_injection()
    for needle in ("drive_gmail", "notion", "research", "apple_events"):
        assert needle in block


def test_unconfigured_external_mcp_flags_missing_env(monkeypatch):
    # Force all external auth env vars unset.
    for var in (
        "NOTION_TOKEN",
        "GOOGLE_WORKSPACE_CLIENT_ID",
        "GOOGLE_WORKSPACE_CLIENT_SECRET",
        "GOOGLE_WORKSPACE_REFRESH_TOKEN",
        "GITHUB_PERSONAL_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    block = tool_inventory.format_for_injection()
    # notion needs NOTION_TOKEN; google_workspace needs three.
    assert "notion: unconfigured" in block
    assert "NOTION_TOKEN" in block
    assert "google_workspace: unconfigured" in block
    assert "GOOGLE_WORKSPACE_REFRESH_TOKEN" in block


def test_configured_external_mcp_reports_configured(monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "ntn_fake")
    block = tool_inventory.format_for_injection()
    assert "notion: configured" in block


def test_no_auth_required_mcp_shows_n_a():
    block = tool_inventory.format_for_injection()
    # apple_events / playwright declare no env block.
    assert "apple_events: n/a" in block
    assert "playwright: n/a" in block


def test_block_explicitly_denies_the_allowlist_concept():
    """The May 20 hallucination's specific fix: the block tells Hikari in
    so many words that there is no Claude Code allowlist applying here."""
    block = tool_inventory.format_for_injection()
    assert "allowlist" in block.lower()
    assert "acceptedits" in block.lower().replace("=", "") or "acceptEdits" in block


@pytest.mark.parametrize("known_group", ["memory", "wiki", "utility"])
def test_in_process_groups_present(known_group):
    """We bucket in-process tools by server prefix. The four core
    server groups must always render."""
    block = tool_inventory.format_for_injection()
    assert f"- {known_group}:" in block


def test_missing_mcp_json_block_still_renders(monkeypatch, tmp_path):
    """If .mcp.json is absent, the block must still render in-process tools
    and the no-allowlist footer. The May 20 hallucination fix depends on
    that footer being present unconditionally."""
    monkeypatch.setattr(tool_inventory, "MCP_JSON_PATH", tmp_path / "does-not-exist.json")
    block = tool_inventory.format_for_injection()
    assert block.startswith("# tools available")
    assert "in-process" in block
    assert "allowlist" in block.lower()  # the no-allowlist footer is the load-bearing line


def test_empty_string_env_var_is_unconfigured(monkeypatch):
    """A `.env` line like `NOTION_TOKEN=` makes os.environ.get return "".
    That must report as unconfigured, not 'configured (empty string)'."""
    monkeypatch.setenv("NOTION_TOKEN", "")
    block = tool_inventory.format_for_injection()
    assert "notion: unconfigured" in block
    assert "NOTION_TOKEN" in block
