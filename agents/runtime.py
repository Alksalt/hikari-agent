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
import re
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

from agents import config as cfg
from storage import db
from tools import codex as codex_tools
from tools import dispatch as dispatch_tools
from tools import memory as memory_tools
from tools import photos as photo_tools
from tools import wiki as wiki_tools

from .external_wrap_hook import make_post_tool_use_hook
from .hooks import defer_gated_tools, inject_memory, log_tool_failure
from .subagents import ALL_AGENTS

REPO_ROOT = Path(__file__).parent.parent
logger = logging.getLogger(__name__)


_SDK_ERROR_PATTERNS = [
    re.compile(r"^\s*failed to authenticate\b", re.IGNORECASE),
    re.compile(r"^\s*api error:\s*\d{3}\b", re.IGNORECASE),
    re.compile(r"^\s*\d{3}:\s*", re.IGNORECASE),
]


def looks_like_sdk_error(text: str) -> bool:
    """True if ``text`` looks like an SDK / Anthropic API error string that
    leaked into an AssistantMessage's TextBlock instead of being raised.

    Observed example 2026-05-20: ``Failed to authenticate. API Error: 401
    The socket connection was closed unexpectedly...`` shipped as a heartbeat
    body. Callers should treat a match as a failure and skip the send.
    """
    if not text:
        return False
    return any(p.search(text) for p in _SDK_ERROR_PATTERNS)

MODEL_PRIMARY = os.environ.get("HIKARI_MODEL") or cfg.get("runtime.model_primary") or "claude-sonnet-4-6"
MODEL_FALLBACK = os.environ.get("HIKARI_MODEL_FALLBACK") or cfg.get("runtime.model_fallback") or "claude-haiku-4-5"

# Per-turn budget for chat-path SDK calls. Used by _build_options /
# run_user_turn / respond defaults AND substituted into the persona prompt
# via _persona().format(max_turns=...). Keep this constant and the
# `{max_turns}` placeholder in CLAUDE.md in lockstep.
DEFAULT_MAX_TURNS = cfg.get("runtime.default_max_turns") or 4

# Phase 6 (extended Phase 13): serialize concurrent stateful SDK calls. User
# messages (``run_user_turn``) and visible proactive jobs
# (``run_visible_proactive``) all share the same ``session_id`` for SDK
# resume. Two SDK subprocesses resuming the same session in parallel would
# fork the conversation state — the lock prevents that race.
# ``run_internal_control`` is stateless (resume=None) and intentionally
# does NOT take the lock.
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
    text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    # Substitute the live turn budget into the persona so the prose stays
    # in lockstep with DEFAULT_MAX_TURNS. .replace() is safer than .format()
    # since CLAUDE.md is hand-edited and a stray `{` would crash startup.
    return text.replace("{max_turns}", str(DEFAULT_MAX_TURNS))


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


# Tools allowlisted on every turn. The ``hikari_utility`` entries are
# auto-derived from ``tools/_registry.discover_utility_tool_names()`` —
# adding a feature folder under ``tools/<name>/`` with ``ALL_TOOLS`` is
# enough; no edit here required. The dedicated-server entries
# (memory/photo/wiki/dispatch/codex) and external MCP wildcards still
# live here because they don't go through the utility registry.
_DEDICATED_AND_EXTERNAL_TOOLS = [
    "Agent",
    # Claude SDK native tools used by the `research` subagent
    # (agents/subagents/research.py). Without these in the parent
    # allowlist the subagent can't invoke web tools at all — failure mode
    # is silent at spawn time.
    "WebFetch",
    "WebSearch",
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
    "mcp__apple_events__*",
    "mcp__github__*",
    "mcp__google_workspace__*",
    "mcp__notion__*",
    "mcp__playwright__*",
    "mcp__duckdb__*",
]


@cache
def _base_allowed_tools() -> list[str]:
    from tools._registry import discover_utility_tool_names
    return list(_DEDICATED_AND_EXTERNAL_TOOLS) + discover_utility_tool_names()


# Back-compat alias: tests and callers may still reference the constant
# name. Resolves via the cached function on first access.
class _AllowedToolsProxy:
    def __iter__(self):
        return iter(_base_allowed_tools())

    def __contains__(self, item):
        return item in _base_allowed_tools()

    def __len__(self):
        return len(_base_allowed_tools())

    def __getitem__(self, i):
        return _base_allowed_tools()[i]


_BASE_ALLOWED_TOOLS = _AllowedToolsProxy()


def allowed_tool_names() -> list[str]:
    """Returns a copy of the per-turn tool allowlist. Public accessor for
    ``agents/tool_inventory.py`` so it doesn't reach into the private
    constant directly."""
    return list(_base_allowed_tools())


def _build_options(*, resume: str | None, max_turns: int = DEFAULT_MAX_TURNS,
                   max_budget_usd: float = 0.50,
                   extra_allowed_tools: list[str] | None = None,
                   inject_memory_enabled: bool = True,
                   ) -> ClaudeAgentOptions:
    allowed = list(_base_allowed_tools())
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
    hooks_dict: dict = {
        "PostToolUseFailure": [HookMatcher(hooks=[log_tool_failure])],
        # Phase 6: intercept gated tools (e.g. dispatch with write) with
        # native SDK defer, replacing the bespoke OOB callback pattern.
        # See agents/hooks.py:defer_gated_tools.
        "PreToolUse": [HookMatcher(hooks=[defer_gated_tools])],
        # Phase 8: wrap untrusted external tool outputs (Gmail / Calendar
        # / Drive / Notion / Web*) via wrap_untrusted before the model
        # sees them. Generic boundary, one hook, config-driven patterns.
        "PostToolUse": [HookMatcher(hooks=[make_post_tool_use_hook()])],
    }
    # Phase 13.1 (Stream K): skip inject_memory for stateless internal-control
    # calls — they don't need persona memory context, and running it wastes
    # tokens + risks a race where the hook overwrites pending_surfaced_*
    # runtime_state keys that the concurrent user turn was about to commit.
    if inject_memory_enabled:
        hooks_dict["UserPromptSubmit"] = [HookMatcher(hooks=[inject_memory])]
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
        hooks=hooks_dict,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        resume=resume,
        permission_mode="acceptEdits",
    )


async def _invoke_sdk(
    prompt: str,
    *,
    resume: str | None,
    log_session_id: bool,
    max_turns: int = DEFAULT_MAX_TURNS,
    max_budget_usd: float = 0.50,
    extra_allowed_tools: list[str] | None = None,
    retry_on_process_error: bool = True,
    inject_memory_enabled: bool = True,
) -> str:
    """Phase 13 (Stream C) — single private SDK invocation helper.

    Owns the ClaudeSDKClient lifecycle, response collection, and the
    ProcessError self-heal retry for paths that resume a stored session.

    Args:
      resume: session_id to resume, or None for a fresh stateless turn.
      log_session_id: whether to write the SDK's returned session_id back to
        the ``session`` table. ``False`` for internal-control / stateless
        calls so they cannot mutate the live chat session.
      retry_on_process_error: when True, a ProcessError on the first attempt
        (with a non-empty resume) clears the stored session and retries
        once with a fresh subprocess. Disabled for stateless calls since
        they never resume.

    Returns the joined assistant text (no DB append — caller appends visible
    outbound text post-send).
    """
    session_id = resume
    parts: list[str] = []
    for attempt in (1, 2):
        options = _build_options(
            resume=session_id,
            max_turns=max_turns,
            max_budget_usd=max_budget_usd,
            extra_allowed_tools=extra_allowed_tools,
            inject_memory_enabled=inject_memory_enabled,
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
                        if log_session_id and msg.session_id:
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
            if (
                retry_on_process_error
                and attempt == 1
                and session_id is not None
            ):
                logger.warning(
                    "SDK subprocess failed with stored session_id=%s; "
                    "clearing and retrying with a fresh session",
                    session_id,
                )
                db.set_session_id("")
                session_id = None
                continue
            raise

    return "".join(parts).strip()


async def run_user_turn(user_text: str) -> str:
    """Real user message. Resumes the live session.

    Returns reply text only — the caller (``telegram_bridge._send_with_choreography``)
    is responsible for appending the FINAL ``text_to_send`` to ``messages``
    after Telegram delivery succeeds. This entrypoint does NOT append the
    assistant reply itself.

    Acquires ``_RUN_LOCK``, resumes ``db.get_session_id()``, updates
    ``session_id`` on the SDK's ResultMessage.
    """
    async with _RUN_LOCK:
        return await _invoke_sdk(
            user_text,
            resume=db.get_session_id(),
            log_session_id=True,
            max_turns=DEFAULT_MAX_TURNS,
            max_budget_usd=0.50,
            retry_on_process_error=True,
        )


async def run_visible_proactive(seed_prompt: str) -> str:
    """Heartbeat / re-engagement / calendar heartbeat content generation.

    Resumes the live session so the proactive message has chat context.
    Returns text only — caller appends the FINAL sent text with
    ``source='proactive'`` after ``send_text`` succeeds.

    Acquires ``_RUN_LOCK``, resumes session_id, updates session_id on
    ResultMessage.
    """
    async with _RUN_LOCK:
        return await _invoke_sdk(
            seed_prompt,
            resume=db.get_session_id(),
            log_session_id=True,
            max_turns=5,
            max_budget_usd=0.20,
            retry_on_process_error=True,
        )


async def run_internal_control(
    prompt: str,
    *,
    max_turns: int = 5,
    max_budget_usd: float = 0.30,
    extra_allowed_tools: list[str] | None = None,
) -> str:
    """Stateless internal control prompt.

    Used by: approval resume, Apple sync, GCal sync, calendar fetch,
    reminder body composition.

    Hard contract: ``resume=None``, no ``session_id`` writeback, no
    ``messages`` append, no handoff write. Returns text only. The live
    Claude SDK session is never touched — control prompts cannot leak
    into the next user turn.

    No ``_RUN_LOCK`` either — stateless turns can't race the live session
    (they don't resume it). No ProcessError retry — without a resume there's
    nothing to self-heal away from.
    """
    return await _invoke_sdk(
        prompt,
        resume=None,
        log_session_id=False,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        extra_allowed_tools=extra_allowed_tools,
        retry_on_process_error=False,
        inject_memory_enabled=False,
    )


async def respond(user_text: str) -> str:
    """Backwards-compat wrapper for the chat path.

    Appends the user message + bumps ``last_user_message``, then delegates
    to ``run_user_turn``. Does NOT append the assistant reply — that's the
    caller's job (telegram_bridge._send_with_choreography, post-send) so the
    DB row matches what Telegram actually delivered (codex P0 fix).
    """
    db.append_message("user", user_text)
    db.runtime_set("last_user_message", db._now())
    return await run_user_turn(user_text)


# Phase 13 (Stream C): legacy alias kept so out-of-stream code that imports
# ``run_proactive`` (e.g. morning_brief) keeps working with the new visible
# proactive semantics. Streams that explicitly compose internal-only prompts
# call ``run_internal_control`` directly.
run_proactive = run_visible_proactive


async def run_isolated_turn(prompt: str, *, max_turns: int = 3,
                            max_budget_usd: float = 0.20) -> str:
    """Single in-character turn without session resume.

    Used by:
      - PersonaEval drift probes (agents.drift_judge.run_persona_probes) —
        runs probe questions against the live persona and compares the
        response to a stored baseline.
      - Anti-sycophancy eval tests (tests/persona/test_sycophancy.py) —
        fires SycEval / ELEPHANT prompts at a fresh persona session and
        scores the response via Haiku.

    Differs from ``run_user_turn`` / ``run_visible_proactive`` in three ways:
      - No session resume — every call is a fresh conversation.
      - No write-back to ``messages``. Probe answers must not pollute the
        chat history.
      - No shared ``_RUN_LOCK`` — these calls never resume the live session,
        so they cannot race with user turns or proactive jobs.

    The full persona + MCP servers + hooks are kept so the response is
    representative of how Hikari actually talks today. That's the whole
    point: SPASM probes measure drift in the *production* persona, not a
    stripped-down test rig.
    """
    options = _build_options(
        resume=None,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
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
                if msg.subtype != "success":
                    logger.warning("isolated turn ended subtype=%s", msg.subtype)
    return "".join(parts).strip()


async def run_reflection_call(prompt: str) -> str:
    """Single LLM call for the daily reflection (no tool use expected).

    Uses a stripped-down ClaudeAgentOptions with a neutral system prompt —
    NOT the Hikari persona — so the model produces raw YAML rather than
    staying in character. No MCP servers, no hooks, no session resume.
    """
    options = ClaudeAgentOptions(
        model=MODEL_PRIMARY,
        fallback_model=MODEL_FALLBACK,
        cwd=str(REPO_ROOT),
        system_prompt=(
            "You are a structured-output assistant. "
            "Follow the instructions in the user message exactly. "
            "Produce only the requested YAML — no prose, no markdown fences "
            "unless the instructions ask for them, no explanations."
        ),
        allowed_tools=[],
        mcp_servers={},
        max_turns=3,
        max_budget_usd=0.30,
        permission_mode="acceptEdits",
        # No resume — reflection is stateless; don't fork the chat session.
        resume=None,
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
                if msg.subtype != "success":
                    logger.warning("reflection call ended subtype=%s", msg.subtype)
    return "".join(parts).strip()
