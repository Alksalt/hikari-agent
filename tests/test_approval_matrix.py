"""Phase 8 — approval-matrix table tests.

Phase 6C update: _is_defer_gated, _tier_for_tool, and _summary_for_defer were
dead code (defer path fully removed in Phase 4F/F). Tests for those symbols have
been deleted. Remaining coverage:

  - subagent prompts do not falsely claim stale approval gates (Codex P1)
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from agents import config
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


def test_subagent_prompts_dont_falsely_claim_approval_gates():
    """Phase 8 guardrail (per Codex P1 finding): subagent prompts must not
    promise approval gates that don't exist in the runtime. The remaining
    gated paths are gmail_send (not yet exposed) and dispatch-with-write.
    Drafts, Notion writes, and wiki_append are NOT gated.

    Phase A: subagent AgentDefinition objects are now sourced from ALL_AGENTS
    (registry-driven) rather than from module-level constants.
    """
    from agents.subagents import ALL_AGENTS

    drive_prompt = ALL_AGENTS["drive_gmail"].prompt.lower()
    notion_prompt = ALL_AGENTS["notion"].prompt.lower()
    wiki_prompt = ALL_AGENTS["wiki"].prompt.lower()

    # Forbidden claims — these are the lies Codex flagged.
    forbidden = ["tier-1", "tier 1", "y to confirm", "y' to confirm"]
    for p in (drive_prompt, notion_prompt, wiki_prompt):
        for phrase in forbidden:
            assert phrase not in p, (
                f"subagent prompt still references stale approval claim: {phrase!r}"
            )
