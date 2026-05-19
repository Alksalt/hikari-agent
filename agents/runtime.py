"""Agent runtime. One ClaudeSDKClient per turn (created fresh, resumed by session_id).

Wires in:
  - System prompt = CLAUDE.md persona (single Sonnet, no router)
  - Project setting source so .claude/skills/ load on-demand
  - Two in-process SDK MCP servers: memory + photos
  - .mcp.json external servers (Google Workspace, when configured)
  - UserPromptSubmit + PostToolUseFailure hooks
  - Bounded turns + budget per call
"""

from __future__ import annotations

import asyncio
import logging
import os
from functools import cache
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    TextBlock,
    create_sdk_mcp_server,
)

from storage import db
from tools import dispatch as dispatch_tools
from tools import memory as memory_tools
from tools import photos as photo_tools
from tools import wiki as wiki_tools

from . import handoff as handoff_mod
from .hooks import defer_gated_tools, inject_memory, log_tool_failure
from .subagents import ALL_AGENTS

REPO_ROOT = Path(__file__).parent.parent
logger = logging.getLogger(__name__)

MODEL_PRIMARY = os.environ.get("HIKARI_MODEL", "claude-sonnet-4-6")
MODEL_FALLBACK = os.environ.get("HIKARI_MODEL_FALLBACK", "claude-haiku-4-5")

# Phase 6: serialize concurrent _run_query calls. User messages, proactive
# heartbeats, the calendar cron, and defer-resume all share the same
# ``session_id`` for SDK resume. Two SDK subprocesses resuming the same session
# in parallel would fork the conversation state — the lock prevents that race.
# Hold time is ~3-15s per turn; that's acceptable given heartbeats are capped
# at ≤4/week by the cadence governor.
_RUN_LOCK = asyncio.Lock()


@cache
def owner_id() -> int:
    raw = os.environ.get("OWNER_TELEGRAM_ID")
    if not raw:
        raise ValueError("OWNER_TELEGRAM_ID not set in environment")
    return int(raw)


@cache
def _persona() -> str:
    return (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")


@cache
def _memory_server():
    return create_sdk_mcp_server(name="hikari_memory", tools=memory_tools.ALL_TOOLS)


@cache
def _photo_server():
    return create_sdk_mcp_server(name="hikari_photo", tools=photo_tools.ALL_TOOLS)


@cache
def _wiki_server():
    # Only the PUBLIC subset (no wiki_append_confirmed). The privileged
    # post-approval tool is on a separate server attached per-resume-turn so
    # its schema isn't even visible to Sonnet on a normal turn.
    return create_sdk_mcp_server(name="hikari_wiki", tools=wiki_tools.PUBLIC_TOOLS)


@cache
def _wiki_confirmed_server():
    """The post-approval execution tools (e.g. ``wiki_append_confirmed``).
    Only attached to ``mcp_servers`` when a defer-resume is in flight."""
    return create_sdk_mcp_server(
        name="hikari_wiki_confirmed", tools=wiki_tools.CONFIRMED_TOOLS,
    )


def _confirmed_tool_names() -> set[str]:
    """Set of fully-qualified tool names that live on the confirmed server.

    Used by ``_build_options`` to decide whether to attach the confirmed
    server based on ``extra_allowed_tools``.
    """
    return {f"mcp__hikari_wiki_confirmed__{t.name}"
            for t in wiki_tools.CONFIRMED_TOOLS}


@cache
def _dispatch_server():
    return create_sdk_mcp_server(name="hikari_dispatch", tools=dispatch_tools.ALL_TOOLS)


def _research_tools():
    # Imported lazily so missing deps (tavily/browser-use/keys) don't break startup
    # for users running without Phase 4 outbound configured.
    try:
        from tools import research as research_tools
        return research_tools.ALL_TOOLS
    except Exception as e:  # noqa: BLE001
        logger.warning("research tools unavailable: %s", e)
        return []


@cache
def _research_server():
    return create_sdk_mcp_server(name="hikari_research", tools=_research_tools())


_BASE_ALLOWED_TOOLS = [
    "Agent",
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
    "mcp__hikari_dispatch__dispatch_claude_session",
    "Read", "Glob", "Grep",
]


def _build_options(*, resume: str | None, max_turns: int = 15,
                   max_budget_usd: float = 0.50,
                   extra_allowed_tools: list[str] | None = None
                   ) -> ClaudeAgentOptions:
    allowed = list(_BASE_ALLOWED_TOOLS)
    if extra_allowed_tools:
        allowed.extend(extra_allowed_tools)
    mcp_servers = {
        "hikari_memory": _memory_server(),
        "hikari_photo": _photo_server(),
        "hikari_wiki": _wiki_server(),
        "hikari_dispatch": _dispatch_server(),
        "hikari_research": _research_server(),
    }
    # Attach the privileged confirmed-tools server only when the resume path
    # explicitly opts in via extra_allowed_tools. On a normal turn the schema
    # for wiki_append_confirmed is not in the MCP manifest at all.
    needed = _confirmed_tool_names()
    if extra_allowed_tools and any(t in needed for t in extra_allowed_tools):
        mcp_servers["hikari_wiki_confirmed"] = _wiki_confirmed_server()
    return ClaudeAgentOptions(
        model=MODEL_PRIMARY,
        fallback_model=MODEL_FALLBACK,
        cwd=str(REPO_ROOT),
        setting_sources=["project"],
        skills="all",
        system_prompt=_persona(),
        agents=ALL_AGENTS,
        mcp_servers=mcp_servers,
        allowed_tools=allowed,
        hooks={
            "UserPromptSubmit": [HookMatcher(hooks=[inject_memory])],
            "PostToolUseFailure": [HookMatcher(hooks=[log_tool_failure])],
            # Phase 6: intercept gated tools (e.g. wiki_append) with native
            # SDK defer, replacing the bespoke OOB callback pattern. See
            # agents/hooks.py:defer_gated_tools.
            "PreToolUse": [HookMatcher(hooks=[defer_gated_tools])],
        },
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        resume=resume,
        permission_mode="acceptEdits",
    )


async def _run_query(prompt: str, *, max_turns: int = 15,
                     max_budget_usd: float = 0.50,
                     log_to_memory: bool = True,
                     extra_allowed_tools: list[str] | None = None) -> str:
    """Send one prompt, collect text response, persist session_id.

    All concurrent callers serialize on ``_RUN_LOCK`` so two SDK subprocesses
    don't resume the same ``session_id`` in parallel (which would fork the
    conversation state). User messages, proactive heartbeats, the calendar
    cron, and the defer-resume path all funnel through here.

    ``extra_allowed_tools`` is used by the defer-resume codepath in
    ``tools/approvals._resume_after_defer`` to inject post-approval sibling
    tools (e.g. ``wiki_append_confirmed``) for one turn only — the next
    normal turn rebuilds options with the base allowlist.
    """
    async with _RUN_LOCK:
        session_id = db.get_session_id()
        options = _build_options(
            resume=session_id,
            max_turns=max_turns,
            max_budget_usd=max_budget_usd,
            extra_allowed_tools=extra_allowed_tools,
        )

        parts: list[str] = []
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            parts.append(block.text)
                elif isinstance(msg, ResultMessage):
                    if msg.session_id:
                        db.set_session_id(msg.session_id)
                    if msg.subtype != "success":
                        logger.warning("agent loop ended subtype=%s", msg.subtype)
                    if msg.deferred_tool_use is not None:
                        logger.info(
                            "SDK halted on deferred tool: %s (id=%s)",
                            msg.deferred_tool_use.name,
                            msg.deferred_tool_use.id,
                        )

        text = "".join(parts).strip()
        if log_to_memory and text:
            db.append_message("assistant", text)
        return text


async def respond(user_text: str) -> str:
    """Main chat entry point — called per Telegram message."""
    db.append_message("user", user_text)
    db.runtime_set("last_user_message", db._now())
    reply = await _run_query(user_text, max_turns=15, max_budget_usd=0.50)
    # Snapshot last turns for next-session cold-open ("where were we").
    try:
        handoff_mod.write_handoff()
    except Exception:
        logger.exception("write_handoff failed (non-fatal)")
    return reply


async def run_proactive(seed_prompt: str) -> str:
    """Generate one proactive message text. Caller is responsible for sending it."""
    return await _run_query(seed_prompt, max_turns=5, max_budget_usd=0.20,
                            log_to_memory=False)


async def run_reflection_call(prompt: str) -> str:
    """Single LLM call for the daily reflection (no tool use expected)."""
    return await _run_query(prompt, max_turns=3, max_budget_usd=0.30,
                            log_to_memory=False)
