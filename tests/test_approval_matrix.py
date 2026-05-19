"""Phase 8 — approval-matrix table tests.

Covers:
  - regex patterns in ``approvals.defer_gated_tools`` match correctly
  - ``approvals.defer_when_args_match`` lets dispatch auto-allow when its
    ``allowed_tools`` arg is read-only, and defer when it includes a write tool
  - tools not in the gated list always auto-allow
  - malformed args (None, missing key, empty string, case variants) handle
    cleanly without raising
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from agents import config, hooks
from storage import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    config.reload()
    yield


@pytest.mark.parametrize(
    "tool_name, tool_input, expected",
    [
        # wiki_append is no longer gated under Phase 8.
        ("mcp__hikari_wiki__wiki_append",
         {"path": "p.md", "content": "x"},
         False),
        # Memory + photo tools never defer.
        ("mcp__hikari_memory__recall", {"query": "x"}, False),
        ("mcp__hikari_photo__generate_photo", {"prompt": "x"}, False),
        # Calendar with attendees auto-runs (per user choice).
        ("mcp__claude_ai_Google_Calendar__create_event",
         {"attendees": ["a@b.com"]},
         False),
        # Notion writes auto-run.
        ("mcp__notion__create_page", {"title": "x"}, False),
        # Read-only dispatch auto-runs.
        ("mcp__hikari_dispatch__dispatch_claude_session",
         {"allowed_tools": "Read,Grep"},
         False),
        ("mcp__hikari_dispatch__dispatch_claude_session",
         {"allowed_tools": "Read,Glob,WebFetch"},
         False),
        # Write dispatch DEFERS.
        ("mcp__hikari_dispatch__dispatch_claude_session",
         {"allowed_tools": "Read,Edit,Bash"},
         True),
        ("mcp__hikari_dispatch__dispatch_claude_session",
         {"allowed_tools": "Write"},
         True),
        ("mcp__hikari_dispatch__dispatch_claude_session",
         {"allowed_tools": "bash"},  # case-insensitive
         True),
        ("mcp__hikari_dispatch__dispatch_claude_session",
         {"allowed_tools": "  Edit  "},  # whitespace tolerated
         True),
        # Future-proof: a gmail send tool added later would match if/when its
        # regex is wired. Not in the gated list today.
        ("mcp__claude_ai_Gmail__send_email",
         {"to": "alice@example.com"},
         False),
    ],
)
def test_is_defer_gated_matrix(tool_name, tool_input, expected):
    assert hooks._is_defer_gated(tool_name, tool_input) is expected


def test_dispatch_with_missing_allowed_tools_arg_auto_allows():
    """If the dispatch arg is missing entirely, treat as read-only default
    (the actual tool body will substitute DEFAULT_ALLOWED_TOOLS)."""
    assert hooks._is_defer_gated(
        "mcp__hikari_dispatch__dispatch_claude_session",
        {"repo_path": "/Users/alt/work_dir/x", "task": "t"},
    ) is False


def test_dispatch_with_none_tool_input_treated_conservatively():
    """A None tool_input on a gated tool falls through to defer (we'd rather
    halt than auto-run when args are unknown)."""
    assert hooks._is_defer_gated(
        "mcp__hikari_dispatch__dispatch_claude_session", None
    ) is True


def test_tier_for_tool_always_returns_2():
    """Phase 8: single-tier. All gated tools use CONFIRM-SEND."""
    assert hooks._tier_for_tool("mcp__hikari_dispatch__dispatch_claude_session") == 2
    assert hooks._tier_for_tool("mcp__hikari_wiki__wiki_append") == 2
    assert hooks._tier_for_tool("anything") == 2


def test_invalid_regex_in_config_does_not_raise(tmp_path, monkeypatch):
    cfg_text = (
        "approvals:\n"
        "  defer_gated_tools: ['[invalid(regex']\n"
        "  defer_when_args_match: {}\n"
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()

    # Should not raise; invalid pattern is logged and skipped.
    assert hooks._is_defer_gated("mcp__hikari_wiki__wiki_append", {}) is False


def test_empty_gated_list_means_no_defer(tmp_path, monkeypatch):
    cfg_text = (
        "approvals:\n"
        "  defer_gated_tools: []\n"
    )
    p = tmp_path / "engagement.yaml"
    p.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("HIKARI_CONFIG_PATH", str(p))
    config.reload()

    assert hooks._is_defer_gated(
        "mcp__hikari_dispatch__dispatch_claude_session",
        {"allowed_tools": "Read,Edit,Bash"},
    ) is False


def test_subagent_prompts_dont_falsely_claim_approval_gates():
    """Phase 8 guardrail (per Codex P1 finding): subagent prompts must not
    promise approval gates that don't exist in the runtime. The remaining
    gated paths are gmail_send (not yet exposed) and dispatch-with-write.
    Drafts, Notion writes, and wiki_append are NOT gated."""
    from agents import subagents

    drive_prompt = subagents.DRIVE_GMAIL_AGENT.prompt.lower()
    notion_prompt = subagents.NOTION_AGENT.prompt.lower()
    wiki_prompt = subagents.WIKI_AGENT.prompt.lower()

    # Forbidden claims — these are the lies Codex flagged.
    forbidden = ["tier-1", "tier 1", "y to confirm", "y' to confirm"]
    for p in (drive_prompt, notion_prompt, wiki_prompt):
        for phrase in forbidden:
            assert phrase not in p, (
                f"subagent prompt still references stale approval claim: {phrase!r}"
            )
