"""Phase 2-8 smoke tests — imports, persona, schema, retrieval, skills present."""

from __future__ import annotations

import importlib
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


def test_persona_present():
    persona_md = REPO_ROOT / "assets" / "PERSONA.md"
    assert persona_md.is_file()
    content = persona_md.read_text()
    assert "Hikari Tsukino" in content
    assert "never end a message asking for tasks" in content


def test_all_skills_present():
    skills_dir = REPO_ROOT / ".claude" / "skills"
    expected = ["character-voice", "recall-memory",
                "schedule-heartbeat", "drive-search"]
    for name in expected:
        skill_md = skills_dir / name / "SKILL.md"
        assert skill_md.is_file(), f"missing skill: {name}"
        content = skill_md.read_text()
        assert content.startswith("---"), f"{name} missing YAML frontmatter"
        assert f"name: {name}" in content


def test_bundled_skill_files():
    skills_dir = REPO_ROOT / ".claude" / "skills"
    assert (skills_dir / "character-voice" / "VOICE_DEPTH.md").is_file()
    assert (skills_dir / "character-voice" / "LORE_CORE.md").is_file()
    assert (skills_dir / "schedule-heartbeat" / "EXAMPLES.md").is_file()


def test_mcp_json_valid():
    import json
    data = json.loads((REPO_ROOT / ".mcp.json").read_text())
    assert "mcpServers" in data


def test_session_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    from storage import db
    importlib.reload(db)
    assert db.get_session_id() is None
    db.set_session_id("sess-abc-123")
    assert db.get_session_id() == "sess-abc-123"


def test_full_schema_present(tmp_path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    from storage import db
    importlib.reload(db)
    db.upsert_core_block("test", "value")
    with db._conn() as c:
        rows = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()}
    expected = {"session", "core_blocks", "facts", "messages", "episodes",
                "tasks", "character_thoughts", "runtime_state", "fts",
                "vec_facts", "vec_episodes",
                # Phase 2 + Phase 3
                "background_tasks", "approvals", "audit_log",
                # Phase 10
                "reminders",
                # Sprint 3-A
                "calendar_notifications",
                # Sprint 5A
                "entities", "entity_aliases", "fact_entities"}
    missing = expected - rows
    assert not missing, f"missing tables: {missing}"


def test_memory_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    from storage import db
    importlib.reload(db)

    fid = db.insert_fact("user", "likes", "cold rice", importance=7, confidence=0.95)
    assert fid > 0
    active = db.active_facts_matching("user", "likes")
    assert len(active) == 1
    assert active[0]["object"] == "cold rice"

    new_fid = db.insert_fact("user", "likes", "hot rice", importance=7, confidence=0.9)
    db.supersede_fact(fid, new_fid, reason="user changed mind")
    active = db.active_facts_matching("user", "likes")
    assert len(active) == 1
    assert active[0]["object"] == "hot rice"


def test_tasks_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    from storage import db
    importlib.reload(db)
    tid = db.create_task("ask about the cabbage")
    assert tid > 0
    assert any(t["id"] == tid for t in db.open_tasks())
    db.update_task(tid, status="completed")
    assert not any(t["id"] == tid for t in db.open_tasks())


def test_retrieval_returns_hits(tmp_path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    from storage import db, retrieval
    importlib.reload(db)
    importlib.reload(retrieval)
    db.insert_fact("user", "works_at", "openai research labs", importance=8)
    db.insert_fact("user", "drinks", "tea then coffee", importance=4)
    db.insert_episode("2026-05-17", "talked about transformer attention papers", importance=6)

    hits = retrieval.legacy_retrieve("openai", limit=5)
    assert len(hits) >= 1
    assert any("openai" in h.text.lower() for h in hits)


def test_scheduler_builds(monkeypatch):
    """Phase 8: ``memory_prune`` is always wired; ``calendar_heartbeat`` is
    gated on the runtime healthy flag / env var. Test the gated-on shape."""
    from agents.scheduler import build_scheduler
    from storage import db as _db_mod

    _db_mod.runtime_set("calendar_heartbeat_healthy", "1")

    async def noop(_t: str) -> None:
        return None

    sched = build_scheduler(noop)
    ids = {j.id for j in sched.get_jobs()}
    # Phase 11: reminders_apple_sync is added on macOS only.
    import sys
    expected = {
        "consolidation",
        "daily_reflection", "memory_prune",
        "reminders_fire", "reminders_gcal_sync",
        # Sprint 1: consolidated daily brief (5-min poll) replaces the old
        # morning_brief (06:00 cron) + daily_checkin (5-min poll) jobs.
        "daily_brief",
        # Phase 11: weekly sleep-time consolidation (Sunday 04:30).
        "weekly_consolidation",
        # 2026-05-20 five-feature batch:
        "evening_diary",     # daily 22:00 — composes data/diary/YYYY-MM-DD.md
        "drift_canary",      # weekly Sunday 20:00 — three hard-opinion probes
        # 2026-05-21 Ghost-of-Future-Self letter (first Sunday of month, 10:00).
        "future_letter",
        # 2026-05-21 Decision-log resolver (weekly Sunday 19:00).
        "decision_resolver",
        # Phase 3 + Sprint 3-A: monthly pruner (messages, oauth_audit_log, drift_probes, calendar_notifications).
        "monthly_prune",
        # Phase I: unified engagement_tick (60s, all producers) — replaces heartbeat/reengage/calendar_heartbeat.
        "engagement_tick",
        # Phase H: MCP warm-pool eviction (every 30s).
        "mcp_warm_pool_evict",
        # Phase 5D: Graphiti outbox drain (every 30s, when GRAPHITI_ENABLED != 'false').
        "graph_outbox_drain",
        "media_outbox_drain",
        # Sprint A: hourly time-of-day texture, daily diary, monthly interests pool refresh.
        "time_texture",
        "diary_writer",
        "interests_refresh",
        # Phase S: annual review ceremony (Dec 26-31, 11:00).
        "annual_review",
    }
    if sys.platform == "darwin":
        expected.add("reminders_apple_sync")
    # Subset check (not exact equality) avoids ordering-sensitive failures when
    # other tests patch config in a full-suite run.
    assert expected.issubset(ids), f"missing jobs: {expected - ids}"


def test_runtime_uses_accept_edits(monkeypatch):
    """Phase 3 dropped bypassPermissions in favor of acceptEdits + tighter allowlist."""
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "0")
    from agents import runtime
    importlib.reload(runtime)
    opts = runtime._build_options(resume=None)
    assert opts.permission_mode == "acceptEdits"


def test_runtime_registers_all_subagents(monkeypatch):
    """All specialist subagents registered:
    wiki, drive_gmail, notion, research, github.
    Phase A removed recall and code_dispatch (functionality served by direct tools).
    Stream D removed apple_events and voice_critic."""
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "0")
    from agents import runtime
    importlib.reload(runtime)
    opts = runtime._build_options(resume=None)
    expected = {"wiki", "drive_gmail", "notion", "research", "github"}
    assert set(opts.agents.keys()) == expected


def test_runtime_has_agent_tool(monkeypatch):
    """The 'Agent' tool must be in allowed_tools or subagents never spawn."""
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "0")
    from agents import runtime
    importlib.reload(runtime)
    opts = runtime._build_options(resume=None)
    assert "Agent" in opts.allowed_tools


def test_runtime_hikari_allowlist_minimal(monkeypatch):
    """Stream A added mcp__google_workspace__* to the main allowlist.
    Stream B removed Read, Glob, Grep (replaced by mcp__hikari_utility__read_attachment)."""
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "0")
    from agents import runtime
    importlib.reload(runtime)
    opts = runtime._build_options(resume=None)
    # Stream A: google_workspace tools are now in the allowlist.
    assert any("google_workspace" in t for t in opts.allowed_tools)
    # Stream B: Read, Glob, Grep removed; read_attachment replaces them.
    assert "Read" not in opts.allowed_tools
    assert "Glob" not in opts.allowed_tools
    assert "Grep" not in opts.allowed_tools
    assert "mcp__hikari_utility__read_attachment" in opts.allowed_tools


def test_background_tasks_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    from storage import db
    importlib.reload(db)
    db.bg_task_create("uuid-1", "claude_session", 99, "do x", meta={"repo": "/tmp"})
    t = db.bg_task_get("uuid-1")
    assert t["status"] == "queued"
    db.bg_task_update("uuid-1", status="running", session_id="sess-x")
    t = db.bg_task_get("uuid-1")
    assert t["status"] == "running"
    assert t["session_id"] == "sess-x"
    assert len(db.bg_tasks_running()) == 1
    db.bg_task_update("uuid-1", status="done", completed_at=db._now(),
                      result_summary="ok", cost_usd=0.42, tool_use_count=5)
    assert len(db.bg_tasks_running()) == 0


def test_approval_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    from storage import db
    importlib.reload(db)
    aid = db.approval_create(99, "wiki_append", 1, "test", {"x": 1})
    assert db.approval_pending_for(99)["id"] == aid
    db.approval_resolve(aid, "approved")
    assert db.approval_pending_for(99) is None


def test_audit_log_hash_chain(tmp_path, monkeypatch):
    """Each audit row's hash_prev = previous row's hash_self."""
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    from storage import db
    importlib.reload(db)
    a1 = db.audit_append("tool1", '{"x":1}', "ok", "owner")
    a2 = db.audit_append("tool2", '{"y":2}', "ok", "owner")
    a3 = db.audit_append("tool3", '{"z":3}', "ok", "owner")
    with db._conn() as c:
        rows = c.execute("SELECT id, hash_prev, hash_self FROM audit_log ORDER BY id").fetchall()
    assert rows[0]["hash_prev"] == ""
    assert rows[1]["hash_prev"] == rows[0]["hash_self"]
    assert rows[2]["hash_prev"] == rows[1]["hash_self"]
    assert a3 > a2 > a1


def test_log_scrub_redacts_secrets():
    import logging

    from agents.log_scrub import RedactingFilter
    msg = (
        "leaked: Bearer abc123def456ghi789jkl secret "
        "sk-ant-abcdefghijklmnopqrstuvwxyz1234567890"
    )
    rec = logging.LogRecord(
        name="x", level=logging.ERROR, pathname="", lineno=0,
        msg=msg,
        args=(), exc_info=None,
    )
    RedactingFilter().filter(rec)
    out = rec.getMessage()
    assert "[REDACTED" in out
    assert "abc123def456ghi789jkl" not in out


def test_dispatch_tool_registered(monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "0")
    from tools import dispatch
    names = [t.name for t in dispatch.ALL_TOOLS]
    assert "dispatch_claude_session" in names


def test_dispatch_rejects_outside_workdir(tmp_path, monkeypatch):
    """Dispatch should reject repos not under WORK_DIR_ROOT."""
    import asyncio
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    from tools import dispatch
    importlib.reload(dispatch)
    dispatch.set_owner_chat_id(12345)
    out = asyncio.run(dispatch.dispatch_claude_session.handler({
        "repo_path": str(tmp_path),
        "task": "do nothing",
        "allowed_tools": "Read",
        "max_turns": 5,
    }))
    text = out["content"][0]["text"]
    assert "outside" in text.lower() or "work_dir" in text.lower()



def test_silence_commands_persist(tmp_path, monkeypatch):
    """set_silence writes silence_until to runtime_state; off=True clears it."""
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    from storage import db
    importlib.reload(db)
    assert db.runtime_get("silence_until") is None
    db.runtime_set("silence_until", "2099-01-01T00:00:00+00:00")
    assert db.runtime_get("silence_until") == "2099-01-01T00:00:00+00:00"
    db.runtime_set("silence_until", None)
    assert db.runtime_get("silence_until") is None
