"""Auto-discovery registry — drops a feature into ``tools/`` and gets
picked up automatically, with the right names threaded through the
runtime allowlist."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    from storage import db
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    from tools._registry import clear_cache
    clear_cache()
    yield


def test_link_shelf_is_discovered():
    from tools._registry import discover_utility_tools

    names = {t.name for t in discover_utility_tools()}
    assert {"link_save", "link_search", "link_list", "link_update", "link_delete"} <= names


def test_existing_utility_tools_still_discovered():
    """Regression guard: the registry must continue to find every flat
    tool module that previously lived in the hardcoded allowlist."""
    from tools._registry import discover_utility_tools

    names = {t.name for t in discover_utility_tools()}
    # A representative subset of the pre-registry hand-maintained list.
    expected = {
        "reminder_create", "reminder_list", "reminder_cancel", "reminder_snooze",
        "note_create", "note_search", "note_read",
        "weather_fetch", "translate", "calc", "python_run",
        "currency_convert", "arxiv_search",
        "places_search", "place_open_now",
        "ytmusic_recent", "ytmusic_search", "ytmusic_library",
        "read_attachment",
    }
    missing = expected - names
    assert not missing, f"registry lost previously-allowlisted tools: {missing}"


def test_allowlist_names_match_mcp_prefix():
    from tools._registry import discover_utility_tool_names

    names = discover_utility_tool_names()
    assert names, "expected at least one utility tool to be discovered"
    for n in names:
        assert n.startswith("mcp__hikari_utility__"), (
            f"discovered name {n!r} missing mcp__hikari_utility__ prefix"
        )


def test_runtime_allowlist_includes_link_shelf():
    """The auto-derived allowlist in ``agents/runtime.py`` should pick
    up new features without any edits to runtime.py itself."""
    from agents.runtime import allowed_tool_names

    names = allowed_tool_names()
    expected = {
        "mcp__hikari_utility__link_save",
        "mcp__hikari_utility__link_search",
        "mcp__hikari_utility__link_list",
        "mcp__hikari_utility__link_update",
        "mcp__hikari_utility__link_delete",
    }
    assert expected <= set(names), (
        f"runtime allowlist missing link_shelf tools: {expected - set(names)}"
    )


def test_allowed_tool_names_no_duplicates():
    """_base_allowed_tools must not contain duplicates after Fix 2 dedup.

    Before the fix, utility tools listed in both tools.yaml and auto-discovered
    by discover_utility_tool_names() appeared twice in the concatenated list.
    """
    from agents.runtime import allowed_tool_names
    names = allowed_tool_names()
    assert len(names) == len(set(names)), (
        f"duplicate tool names in allowlist: "
        f"{[n for n in names if names.count(n) > 1]}"
    )


def test_dedicated_server_modules_not_in_utility():
    """The registry must NOT pull memory/photos/wiki/dispatch/codex into
    hikari_utility — those have their own MCP servers."""
    from tools._registry import discover_utility_tools

    names = {t.name for t in discover_utility_tools()}
    # Picking a representative tool from each dedicated server.
    forbidden = {"recall", "remember", "wiki_search",
                 "dispatch_claude_session", "list_codex_reports"}
    leaked = forbidden & names
    assert not leaked, (
        f"dedicated-server tools leaked into hikari_utility: {leaked}"
    )
