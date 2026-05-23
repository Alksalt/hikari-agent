"""Phase B — inject_memory block culling.

Priority table:
  1 (always-on): now, working_memory, gap_since_last, core_blocks,
                 peer_representation, open_tasks
  2 (conditional): affect, unresolved_decisions, callback_candidate,
                   tools_available
  3 (conditional, first cut): lexicon, location, observations, noticings,
                               session_handoff
"""

from __future__ import annotations

import asyncio
import importlib
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from agents import config


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    config.reload()
    yield
    config.reload()


@pytest.fixture()
def patched_config(tmp_path, monkeypatch):
    """Return a helper that writes a partial YAML override into a temp file
    and reloads config from it. Caller can call it with a dict of overrides."""
    import copy

    base_yaml_path = Path(__file__).parent.parent / "config" / "engagement.yaml"
    with base_yaml_path.open() as f:
        base = yaml.safe_load(f)

    def _apply(overrides: dict) -> None:
        merged = copy.deepcopy(base)
        for k, v in overrides.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v
        out = tmp_path / "engagement_override.yaml"
        out.write_text(yaml.dump(merged))
        monkeypatch.setenv("HIKARI_CONFIG_PATH", str(out))
        config.reload()

    yield _apply
    config.reload()


def _call_inject(user_prompt: str = "hi") -> dict:
    from agents.hooks import inject_memory
    return asyncio.run(inject_memory({"prompt": user_prompt}, None, None))


def _ctx(result: dict) -> str:
    return result.get("hookSpecificOutput", {}).get("additionalContext", "")


def test_six_always_on_blocks_called_unconditionally():
    """Priority-1 helpers called unconditionally; blocks present when state exists."""
    from storage import db

    db.upsert_core_block("mood_today", "focused")
    db.create_task("open thing")
    db.upsert_peer_representation({"summary": "curious, technical"})
    db.append_message("user", "hello", source="chat")
    db.append_message("assistant", "hey", source="chat")

    result = _call_inject()
    ctx = _ctx(result)

    assert "# now" in ctx, "# now must always be present"
    assert "# memory: core" in ctx, "core_blocks must always be present"
    assert "# memory: open tasks" in ctx, "open_tasks must always be present"
    assert "# memory: who they are" in ctx, (
        "peer_representation must always be present when populated"
    )
    assert "# working_memory" in ctx, (
        "working_memory renders when there are prior messages"
    )


def test_priority_3_blocks_cut_on_overflow(patched_config):
    """When priority-3 blocks are huge and cap is tight, they are dropped."""
    from storage import db

    db.upsert_core_block("mood_today", "focused")
    db.create_task("open thing")
    db.upsert_peer_representation({"summary": "x"})

    big_phrase = "longphrase" * 300
    db.lexicon_record(big_phrase, source="user_coined", weight=1.0)
    db.lexicon_record(big_phrase, source="user_coined", weight=1.0)
    db.lexicon_record(big_phrase, source="user_coined", weight=1.0)

    patched_config({"memory": {"additional_context_max_chars": 500}})

    result = _call_inject()
    ctx = _ctx(result)

    assert "# now" in ctx, "# now (priority-1) must survive overflow"
    assert "# memory: core" in ctx, "core_blocks (priority-1) must survive overflow"

    assert big_phrase not in ctx, "huge lexicon block (priority-3) must be cut"


def test_priority_1_blocks_always_render_even_if_over_cap(patched_config):
    """Priority-1 blocks render even when combined size exceeds the cap."""
    from storage import db

    fat_summary = "x" * 5000
    db.upsert_core_block("mood_today", "focused")
    db.create_task("open thing")
    db.upsert_peer_representation({"summary": fat_summary})

    patched_config({"memory": {"additional_context_max_chars": 100}})

    result = _call_inject()
    ctx = _ctx(result)

    assert "# now" in ctx, "# now must render even past cap"
    assert "# memory: core" in ctx, "core_blocks must render even past cap"
    assert "# memory: who they are" in ctx, "peer_representation must render even past cap"

    assert len(ctx) > 100, "priority-1 blocks are allowed to exceed the soft cap"


def test_per_block_disable_via_config_works(patched_config):
    """Setting memory.conditional_blocks.tools_available.enabled=false removes block."""
    from storage import db

    db.upsert_core_block("mood_today", "focused")

    patched_config({
        "memory": {
            "conditional_blocks": {
                "tools_available": {"enabled": False},
            }
        }
    })

    with patch("agents.tool_inventory.format_for_injection",
               return_value="# tools available\nsome tools"):
        result = _call_inject()
        ctx = _ctx(result)

    assert "# tools available" not in ctx, (
        "tools_available block must be absent when disabled via config"
    )


def test_cap_respects_default_4096():
    """Default memory.additional_context_max_chars is 4096."""
    val = config.get("memory.additional_context_max_chars", None)
    assert val is not None, "memory.additional_context_max_chars must be set in config"
    assert int(val) == 4096, f"expected 4096, got {val}"


def test_block_order_preserved():
    """Blocks appear in original insertion order, not priority order."""
    from storage import db

    db.upsert_core_block("mood_today", "focused")
    db.create_task("open thing")
    db.upsert_peer_representation({"summary": "detail"})
    db.append_message("user", "hey there", source="chat")
    db.append_message("assistant", "hi", source="chat")

    result = _call_inject()
    ctx = _ctx(result)

    now_pos = ctx.find("# now")
    core_pos = ctx.find("# memory: core")
    tasks_pos = ctx.find("# memory: open tasks")

    assert now_pos != -1, "# now must be present"
    assert core_pos != -1, "# memory: core must be present"
    assert tasks_pos != -1, "# memory: open tasks must be present"

    assert now_pos < core_pos, "# now must come before core_blocks"
    assert core_pos < tasks_pos, "core_blocks must come before open_tasks"
