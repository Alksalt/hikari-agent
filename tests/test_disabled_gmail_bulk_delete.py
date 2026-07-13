"""The Gmail bulk-delete capability must not be exposed to Hikari."""

from pathlib import Path


BULK_DELETE = "mcp__google_workspace__gmail_bulk_delete_messages"
GOOGLE_WILDCARD = "mcp__google_workspace__*"
ROOT = Path(__file__).resolve().parent.parent


def test_bulk_delete_has_no_registry_or_runtime_allowlist_entry():
    from agents.runtime import allowed_tool_names
    from tools._tools_yaml import load_registry

    registry = load_registry()
    assert registry._resolve(BULK_DELETE) is None
    assert BULK_DELETE not in registry.allowed_tool_names()
    assert GOOGLE_WILDCARD not in registry.allowed_tool_names()
    assert BULK_DELETE not in allowed_tool_names()
    assert GOOGLE_WILDCARD not in allowed_tool_names()


def test_drive_gmail_subagent_uses_explicit_allowlist_without_bulk_delete():
    from tools._tools_yaml import load_registry

    tools = load_registry()._subagents_spec["drive_gmail"].tools
    assert BULK_DELETE not in tools
    assert GOOGLE_WILDCARD not in tools
    assert all(not (entry.endswith("*") and BULK_DELETE.startswith(entry[:-1])) for entry in tools)


def test_bulk_delete_is_absent_from_catalog_and_drive_gmail_prompt():
    from tools.catalog import get_catalog

    assert BULK_DELETE not in {entry.name for entry in get_catalog().entries}
    prompt = (ROOT / "agents/subagents/prompts/drive_gmail.prompt.md").read_text(
        encoding="utf-8"
    )
    assert "gmail_bulk_delete_messages" not in prompt
