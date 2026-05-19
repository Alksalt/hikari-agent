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
    ProcessError,
    ResultMessage,
    TextBlock,
    create_sdk_mcp_server,
)

from storage import db
from tools import codex as codex_tools
from tools import dispatch as dispatch_tools
from tools import memory as memory_tools
from tools import photos as photo_tools
from tools import wiki as wiki_tools

from . import handoff as handoff_mod
from .external_wrap_hook import make_post_tool_use_hook
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
    # Phase 8: wiki_append no longer has an approval gate, so there are no
    # privileged sibling tools to hide behind a conditional server. All public
    # wiki tools live here.
    return create_sdk_mcp_server(name="hikari_wiki", tools=wiki_tools.PUBLIC_TOOLS)


@cache
def _dispatch_server():
    return create_sdk_mcp_server(name="hikari_dispatch", tools=dispatch_tools.PUBLIC_TOOLS)


@cache
def _codex_server():
    """Phase 8: small MCP server that exposes the codex/ review reports to
    Hikari (list + read). Wrapped as untrusted on read."""
    return create_sdk_mcp_server(name="hikari_codex", tools=codex_tools.ALL_TOOLS)


@cache
def _utility_server():
    """Phase 10: combined MCP server hosting weather, reminders, translation,
    calc, currency, arxiv, places, ytmusic. Each feature contributes tools to
    tools._utility_index.ALL_TOOLS."""
    from tools import _utility_index
    return create_sdk_mcp_server(name="hikari_utility", tools=_utility_index.ALL_TOOLS)


@cache
def _dispatch_confirmed_server():
    """The post-approval execution tool for the dispatch arg-gate
    (`dispatch_claude_session_confirmed`). Attached to ``mcp_servers`` only
    during a defer-resume turn so its schema isn't in the manifest otherwise."""
    return create_sdk_mcp_server(
        name="hikari_dispatch_confirmed", tools=dispatch_tools.CONFIRMED_TOOLS,
    )


def _confirmed_tool_names() -> set[str]:
    """Set of fully-qualified tool names that live on a confirmed server.

    Used by ``_build_options`` to decide whether to attach a confirmed
    server based on ``extra_allowed_tools``. Phase 8: only the dispatch
    arg-gate uses this mechanism.
    """
    return {f"mcp__hikari_dispatch_confirmed__{t.name}"
            for t in dispatch_tools.CONFIRMED_TOOLS}


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
    "mcp__hikari_codex__list_codex_reports",
    "mcp__hikari_codex__read_codex_report",
    # Phase 10 utility tools (appended per feature during Phase 1 parallel work).
    # DO NOT remove this anchor comment — Phase 1 agents look for it.
    "mcp__hikari_utility__reminder_create",
    "mcp__hikari_utility__reminder_list",
    "mcp__hikari_utility__reminder_cancel",
    "mcp__hikari_utility__reminder_snooze",
    "mcp__hikari_utility__weather_fetch",
    "mcp__hikari_utility__translate",
    "mcp__hikari_utility__calc",
    "mcp__hikari_utility__python_run",
    "mcp__hikari_utility__currency_convert",
    "mcp__hikari_utility__arxiv_search",
    "mcp__hikari_utility__places_search",
    "mcp__hikari_utility__place_open_now",
    "mcp__hikari_utility__ytmusic_recent",
    "mcp__hikari_utility__ytmusic_search",
    "mcp__hikari_utility__ytmusic_library",
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
        "hikari_codex": _codex_server(),
        "hikari_utility": _utility_server(),
    }
    # Phase 8: attach the privileged dispatch-confirmed server only when the
    # resume path explicitly opts in via extra_allowed_tools. On a normal turn
    # the schema for dispatch_claude_session_confirmed is not in the MCP
    # manifest at all.
    needed = _confirmed_tool_names()
    if extra_allowed_tools and any(t in needed for t in extra_allowed_tools):
        mcp_servers["hikari_dispatch_confirmed"] = _dispatch_confirmed_server()
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
            # Phase 6: intercept gated tools (e.g. dispatch with write) with
            # native SDK defer, replacing the bespoke OOB callback pattern.
            # See agents/hooks.py:defer_gated_tools.
            "PreToolUse": [HookMatcher(hooks=[defer_gated_tools])],
            # Phase 8: wrap untrusted external tool outputs (Gmail / Calendar
            # / Drive / Notion / Web*) via wrap_untrusted before the model
            # sees them. Generic boundary, one hook, config-driven patterns.
            "PostToolUse": [HookMatcher(hooks=[make_post_tool_use_hook()])],
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
    tools (e.g. ``dispatch_claude_session_confirmed``) for one turn only —
    the next normal turn rebuilds options with the base allowlist.
    """
    async with _RUN_LOCK:
        session_id = db.get_session_id()
        # If the stored session is unknown to the bundled CLI (cleared cache,
        # cross-host DB copy, expired) the SDK subprocess exits with code 1
        # before we ever send the prompt. Drop the bad ID and retry once with
        # a fresh session so the bot self-heals instead of stalling on
        # "(brain hit a wall)".
        parts: list[str] = []
        for attempt in (1, 2):
            options = _build_options(
                resume=session_id,
                max_turns=max_turns,
                max_budget_usd=max_budget_usd,
                extra_allowed_tools=extra_allowed_tools,
            )
            parts = []
            try:
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
                break
            except ProcessError:
                if attempt == 1 and session_id is not None:
                    logger.warning(
                        "SDK subprocess failed with stored session_id=%s; "
                        "clearing and retrying with a fresh session",
                        session_id,
                    )
                    db.set_session_id("")
                    session_id = None
                    continue
                raise

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
