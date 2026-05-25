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

import httpx

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ProcessError,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
)

from agents import config as cfg
from storage import db
from tools import codex as codex_tools
from tools import dispatch as dispatch_tools
from tools import memory as memory_tools
from tools import photos as photo_tools
from tools import router as router_tools
from tools import wiki as wiki_tools

from . import sdk_pool

# Re-exported for backwards compatibility — the contextvar itself lives in
# ``agents._turn_state`` so importlib.reload(agents.runtime) (used by
# allowlist tests) doesn't create a new ContextVar object and silently break
# the chat-path fabrication backstop.
from ._turn_state import LAST_TURN_TOOL_NAMES  # noqa: F401
from .external_wrap_hook import make_post_tool_use_hook
from .hooks import defer_gated_tools, inject_memory, log_tool_failure

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

MODEL_PRIMARY = (
    os.environ.get("HIKARI_MODEL") or cfg.get("runtime.model_primary") or "claude-sonnet-4-6"
)
MODEL_FALLBACK = (
    os.environ.get("HIKARI_MODEL_FALLBACK")
    or cfg.get("runtime.model_fallback")
    or "claude-haiku-4-5"
)

_AUX_REFLECTION_SYSTEM = (
    "You are a structured-output assistant. "
    "Follow the instructions in the user message exactly. "
    "Produce only the requested YAML — no prose, no markdown fences "
    "unless the instructions ask for them, no explanations."
)


def _aux_provider() -> str:
    return str(cfg.get("aux_model.provider", "haiku_subscription"))


def _aux_model_id() -> str:
    return str(cfg.get("aux_model.model", "deepseek/deepseek-v4-flash"))


def _aux_sdk_model() -> str:
    """SDK model ID to use for internal-control / SDK-based reflection calls."""
    if _aux_provider() == "haiku_subscription":
        return "claude-haiku-4-5"
    # openrouter path not usable with SDK tools — fall back to haiku subscription
    return "claude-haiku-4-5"


async def _call_aux_llm(prompt: str, *, system: str = _AUX_REFLECTION_SYSTEM) -> str:
    """Cheap LLM call for reflection and other no-tool ops.

    Routes to OpenRouter (httpx) when provider=openrouter, otherwise uses
    the Claude SDK with the haiku subscription model.
    """
    if _aux_provider() == "openrouter":
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if api_key:
            messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
            for attempt in (1, 2):
                try:
                    async with httpx.AsyncClient(timeout=60.0) as _client:
                        resp = await _client.post(
                            "https://openrouter.ai/api/v1/chat/completions",
                            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                            json={"model": _aux_model_id(), "messages": messages, "max_tokens": 2048},
                        )
                except httpx.RequestError as exc:
                    # Transport-level failure (DNS, TCP, TLS, timeout). Most common
                    # OpenRouter failure mode — must be retried like 429/503.
                    if attempt == 1:
                        logger.warning(
                            "aux_llm: openrouter transport error on attempt 1 (%s) — retrying in 2s",
                            type(exc).__name__,
                        )
                        await asyncio.sleep(2.0)
                        continue
                    logger.warning(
                        "aux_llm: openrouter transport error on attempt 2 (%s) — giving up",
                        type(exc).__name__,
                    )
                    raise
                if resp.status_code in (429, 503) and attempt == 1:
                    logger.warning(
                        "aux_llm: openrouter %s on attempt 1 — body=%r — retrying in 2s",
                        resp.status_code, resp.text[:200],
                    )
                    await asyncio.sleep(2.0)
                    continue
                if resp.status_code >= 400:
                    logger.warning(
                        "aux_llm: openrouter HTTP %s — body=%r",
                        resp.status_code, resp.text[:200],
                    )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"].strip()
        logger.warning("aux_model.provider=openrouter but OPENROUTER_API_KEY not set; using haiku")

    opts = ClaudeAgentOptions(
        model="claude-haiku-4-5",
        cwd=str(REPO_ROOT),
        system_prompt=system,
        allowed_tools=[],
        mcp_servers={},
        max_turns=3,
        max_budget_usd=0.10,
        permission_mode="acceptEdits",
        resume=None,
    )
    parts: list[str] = []
    async with ClaudeSDKClient(options=opts) as _sdk:
        await _sdk.query(prompt)
        async for msg in _sdk.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        parts.append(block.text)
    return "".join(parts).strip()


def _inject_keychain_tokens_to_env() -> None:
    """Read provider tokens from keychain and inject into os.environ.

    The .mcp.json uses ${ENV_VAR} substitution resolved from the process
    environment when the SDK spawns MCP subprocesses. Injecting here (once
    at startup) means keychain tokens flow into all subsequent MCP spawns
    without polluting the global env on .env-only deployments.

    Only writes env vars whose current value is empty/unset, so explicit
    .env values always win (backwards-compat).

    Called once at module import time; safe to call again (idempotent via
    the env-var presence check).
    """
    try:
        from auth.google import read_grant_from_keychain
        grant = read_grant_from_keychain()
        if grant:
            for env_var, key in [
                ("GOOGLE_WORKSPACE_CLIENT_ID", "client_id"),
                ("GOOGLE_WORKSPACE_CLIENT_SECRET", "client_secret"),
                ("GOOGLE_WORKSPACE_REFRESH_TOKEN", "refresh_token"),
            ]:
                kc_val = grant.get(key)
                if kc_val:
                    if os.environ.get(env_var):
                        logger.warning(
                            "auth: both keychain and env set for %s — env wins. "
                            "Delete %s from .env after migration to use the keychain value.",
                            env_var, env_var,
                        )
                    else:
                        os.environ[env_var] = str(kc_val)
    except Exception:
        logger.debug("_inject_keychain_tokens_to_env: google read failed (non-fatal)")

    try:
        from auth.notion import _load_token
        token = _load_token()
        kc_notion = token.get("access_token") if token else None
        if kc_notion:
            if os.environ.get("NOTION_TOKEN"):
                logger.warning(
                    "auth: both keychain and env set for %s — env wins. "
                    "Delete %s from .env after migration to use the keychain value.",
                    "NOTION_TOKEN", "NOTION_TOKEN",
                )
            else:
                os.environ["NOTION_TOKEN"] = str(kc_notion)
    except Exception:
        logger.debug("_inject_keychain_tokens_to_env: notion read failed (non-fatal)")

    try:
        from auth.github import _load_pat
        blob = _load_pat()
        kc_gh = blob.get("token") if blob else None
        if kc_gh:
            if os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN"):
                logger.warning(
                    "auth: both keychain and env set for %s — env wins. "
                    "Delete %s from .env after migration to use the keychain value.",
                    "GITHUB_PERSONAL_ACCESS_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN",
                )
            else:
                os.environ["GITHUB_PERSONAL_ACCESS_TOKEN"] = str(kc_gh)
    except Exception:
        logger.debug("_inject_keychain_tokens_to_env: github read failed (non-fatal)")


_inject_keychain_tokens_to_env()

# Phase H: boot-time BM25 index + TTL config. Both are fast (no I/O beyond
# reading tools.yaml once) and must be ready before the first user turn so
# tool_search never hits a cold index mid-conversation.
try:
    from tools.router.tool_search import rebuild_index as _rebuild_router_index
    _rebuild_router_index()
except Exception:
    logger.exception("runtime: failed to pre-build BM25 router index (non-fatal)")

try:
    from agents.mcp_manager import configure_from_registry as _configure_mcp_manager
    _configure_mcp_manager()
except Exception:
    logger.exception("runtime: failed to configure mcp_manager TTLs (non-fatal)")

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
def _router_server():
    """Phase H: BM25 tool-search server. Exposes tool_search so the model can
    find bucket-2/3 tools without having all their definitions in context."""
    return create_sdk_mcp_server(name="hikari_router", tools=router_tools.ALL_TOOLS)


# _DEDICATED_AND_EXTERNAL_TOOLS was deleted in Phase A (step 5).
# The single source of truth is now config/tools.yaml, loaded via
# tools._tools_yaml.load_registry().allowed_tool_names().
# Utility tool names remain auto-discovered from tools._registry.


@cache
def _base_allowed_tools() -> list[str]:
    from tools._registry import discover_utility_tool_names
    from tools._tools_yaml import load_registry
    yaml_tools = load_registry().allowed_tool_names()
    seen = set(yaml_tools)
    deduped_utility = [t for t in discover_utility_tool_names() if t not in seen]
    return yaml_tools + deduped_utility


def allowed_tool_names() -> list[str]:
    """Returns a copy of the per-turn tool allowlist. Public accessor for
    ``agents/tool_inventory.py`` so it doesn't reach into the private
    constant directly."""
    return list(_base_allowed_tools())


def _build_options(*, resume: str | None, max_turns: int = DEFAULT_MAX_TURNS,
                   max_budget_usd: float | None = 0.50,
                   extra_allowed_tools: list[str] | None = None,
                   inject_memory_enabled: bool = True,
                   model: str | None = None,
                   ) -> ClaudeAgentOptions:
    allowed = list(_base_allowed_tools())
    if extra_allowed_tools:
        allowed.extend(extra_allowed_tools)

    # Phase A (step 6): build mcp_servers from the registry.
    # Bucket-1 servers are attached by calling their runtime_factory function.
    # Conditional servers are attached only when extra_allowed_tools intersects
    # the set of tool names that live on that server.
    import importlib

    from tools._tools_yaml import load_registry

    registry = load_registry()
    mcp_servers: dict = {}
    extra_set: set[str] = set(extra_allowed_tools or [])

    for server_name, spec in registry.mcp_servers().items():
        if spec.bucket != 1:
            continue  # bucket-3 external servers are wired via .mcp.json
        if not spec.runtime_factory:
            continue
        if spec.conditional:
            # Attach only when extra_allowed_tools contains a tool on this server.
            # The server's tools are those whose spec.server == server_name.
            server_tools: set[str] = set()
            for tspec in registry.specs():
                if tspec.server == server_name:
                    server_tools.add(tspec.id)
            if not (extra_set & server_tools):
                continue
        # Resolve the factory: "module:callable"
        module_path, fn_name = spec.runtime_factory.rsplit(":", 1)
        mod = importlib.import_module(module_path)
        factory = getattr(mod, fn_name)
        mcp_servers[server_name] = factory()
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
    # Phase E: wire the gatekeeper can_use_tool hook. Imported lazily so tests
    # that mock the registry still work; the callable itself is stateless.
    from tools.gatekeeper_can_use_tool import gatekeeper_can_use_tool
    return ClaudeAgentOptions(
        model=model or MODEL_PRIMARY,
        fallback_model=MODEL_FALLBACK,
        cwd=str(REPO_ROOT),
        setting_sources=["project"],
        skills="all",
        system_prompt=_persona(),
        agents=registry.subagents(),
        mcp_servers=mcp_servers,
        allowed_tools=allowed,
        hooks=hooks_dict,
        can_use_tool=gatekeeper_can_use_tool,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        resume=resume,
        permission_mode="acceptEdits",
    )


async def _invoke_sdk_persistent_live(
    prompt: str | list[dict],
    *,
    log_session_id: bool,
) -> str:
    """Persistent-client path for run_user_turn + run_visible_proactive.

    Uses sdk_pool.get_live_client() — no fresh subprocess fork.
    On ProcessError or TimeoutError: clears suspect session if applicable,
    reconnects once, retries once, then re-raises on second failure.
    On ResultMessage: stores session_id if log_session_id=True.
    """
    sdk_turn_timeout = float(cfg.get("runtime.sdk_turn_timeout_s", 90))
    tool_names_this_turn: set[str] = set()
    LAST_TURN_TOOL_NAMES.set(tool_names_this_turn)

    async def _run_one() -> str:
        client = await sdk_pool.get_live_client()
        parts: list[str] = []

        if isinstance(prompt, list):
            blocks = prompt

            async def _stream_blocks():
                yield {
                    "type": "user",
                    "message": {"role": "user", "content": blocks},
                    "parent_tool_use_id": None,
                }

            await client.query(_stream_blocks())
        else:
            await client.query(prompt)

        async def _collect():
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            parts.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            tool_names_this_turn.add(str(block.name))
                elif isinstance(msg, ResultMessage):
                    if log_session_id and msg.session_id:
                        db.set_session_id(msg.session_id)
                    if msg.subtype != "success":
                        logger.warning("agent loop ended subtype=%s", msg.subtype)
                    if msg.usage:
                        logger.info(
                            "sdk_usage(persistent): in=%s cache_create=%s cache_read=%s out=%s",
                            msg.usage.get("input_tokens", "-"),
                            msg.usage.get("cache_creation_input_tokens", "-"),
                            msg.usage.get("cache_read_input_tokens", "-"),
                            msg.usage.get("output_tokens", "-"),
                        )

        await asyncio.wait_for(_collect(), timeout=sdk_turn_timeout)
        return "".join(parts).strip()

    try:
        result = await _run_one()
    except (TimeoutError, ProcessError) as exc:
        reason = type(exc).__name__
        # If a stored session_id was involved, clear it (suspect session).
        stored = db.get_session_id()
        if stored:
            logger.warning(
                "_invoke_sdk_persistent_live: %s"
                " — clearing suspect session_id (present=True) and reconnecting",
                reason,
            )
            db.set_session_id("")
        else:
            logger.warning(
                "_invoke_sdk_persistent_live: %s — reconnecting live client",
                reason,
            )
        await sdk_pool._reconnect_live(f"{reason} on user turn", lock_run=False)
        result = await _run_one()   # one retry; re-raises on second failure

    sdk_pool._maybe_schedule_live_recycle()
    return result


async def _invoke_sdk(
    prompt: str | list[dict],
    *,
    resume: str | None,
    log_session_id: bool,
    max_turns: int = DEFAULT_MAX_TURNS,
    max_budget_usd: float = 0.50,
    extra_allowed_tools: list[str] | None = None,
    retry_on_process_error: bool = True,
    inject_memory_enabled: bool = True,
    use_persistent_live: bool = False,
    model: str | None = None,
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
    if use_persistent_live and sdk_pool.is_live_persistent_path_enabled():
        return await _invoke_sdk_persistent_live(
            prompt, log_session_id=log_session_id,
        )

    session_id = resume
    parts: list[str] = []
    # Fresh per-call set of tool names — overwrites any stale value from a
    # prior turn in this Context. Read by ``agents.post_filter`` on the chat
    # path to detect hallucinated external-data results.
    tool_names_this_turn: set[str] = set()
    LAST_TURN_TOOL_NAMES.set(tool_names_this_turn)
    for attempt in (1, 2):
        options = _build_options(
            resume=session_id,
            max_turns=max_turns,
            max_budget_usd=max_budget_usd,
            extra_allowed_tools=extra_allowed_tools,
            inject_memory_enabled=inject_memory_enabled,
            model=model,
        )
        parts = []
        try:
            async with ClaudeSDKClient(options=options) as client:
                if isinstance(prompt, list):
                    # SDK's query() accepts str | AsyncIterable[dict]; for a
                    # content-block list, wrap into a single user-message
                    # envelope and yield from an async generator.
                    blocks = prompt

                    async def _stream_blocks():
                        yield {
                            "type": "user",
                            "message": {"role": "user", "content": blocks},
                            "parent_tool_use_id": None,
                        }

                    await client.query(_stream_blocks())
                else:
                    await client.query(prompt)
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                parts.append(block.text)
                            elif isinstance(block, ToolUseBlock):
                                tool_names_this_turn.add(str(block.name))
                    elif isinstance(msg, ResultMessage):
                        if log_session_id and msg.session_id:
                            db.set_session_id(msg.session_id)
                        if msg.subtype != "success":
                            logger.warning("agent loop ended subtype=%s", msg.subtype)
                        # Cache telemetry — the Claude Code CLI already caches
                        # the system prompt + MCP tool definitions
                        # transparently when forwarding to the Anthropic API.
                        # Measured 2026-05-21: in=10 raw / cache_read=16228 /
                        # cache_create=9464 per turn (≈50% input savings,
                        # higher once tool definitions stop being re-written).
                        # Surfacing the numbers here so cache hit ratio is
                        # trackable; alerts can fan out from log scrapes.
                        if msg.usage:
                            logger.info(
                                "sdk_usage: in=%s cache_create=%s cache_read=%s out=%s",
                                msg.usage.get("input_tokens", "-"),
                                msg.usage.get("cache_creation_input_tokens", "-"),
                                msg.usage.get("cache_read_input_tokens", "-"),
                                msg.usage.get("output_tokens", "-"),
                            )
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
            use_persistent_live=True,
        )


async def run_user_turn_blocks(content_blocks: list[dict]) -> str:
    """Variant of run_user_turn that accepts a pre-built list of content blocks
    instead of a plain string. Used by handle_document for PDF/image/HTML ingest
    so the next user turn contains a native ``document`` or ``image`` block,
    not a base64 blob wedged into text.

    The ephemeral path handles content-block lists correctly. log_session_id=True
    so the session_id produced by this turn is stored and the next text turn can
    reference the same conversation (PDF continuity fix). After the turn,
    advance the persistent live client so it resumes from the new session_id.
    """
    async with _RUN_LOCK:
        result = await _invoke_sdk(
            content_blocks,
            resume=db.get_session_id(),
            log_session_id=True,
            max_turns=DEFAULT_MAX_TURNS,
            max_budget_usd=0.50,
            retry_on_process_error=True,
        )
        if sdk_pool.is_live_persistent_path_enabled():
            try:
                await sdk_pool._reconnect_live(
                    "content-block turn advanced session", lock_run=False,
                )
            except Exception:
                logger.warning(
                    "run_user_turn_blocks: live client reconnect after content-block "
                    "turn failed (non-fatal — next text turn will reconnect)",
                    exc_info=True,
                )
        return result


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
            use_persistent_live=True,
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
        model=_aux_sdk_model(),
    )


async def respond(
    user_text: str, *, internal_belief_context: str | None = None
) -> str:
    """Chat path entry point.

    Persists the RAW user text to messages, then builds the SDK prompt.
    When internal_belief_context is provided (belief-frame adversarial suffix),
    the prompt passed to the SDK is augmented but the persisted row stays clean.
    """
    mid = db.append_message("user", user_text)
    db.runtime_set("last_user_message", db._now())
    db.runtime_set("last_user_message_id", str(mid))
    if internal_belief_context:
        sdk_prompt = internal_belief_context + "\n\n" + user_text
    else:
        sdk_prompt = user_text
    return await run_user_turn(sdk_prompt)


# Phase 13 (Stream C): legacy alias kept so out-of-stream code that imports
# ``run_proactive`` (e.g. morning_brief) keeps working with the new visible
# proactive semantics. Streams that explicitly compose internal-only prompts
# call ``run_internal_control`` directly.
run_proactive = run_visible_proactive


async def run_isolated_turn(prompt: str, *, max_turns: int = 3,
                            max_budget_usd: float = 0.20) -> str:
    """Single in-character turn without session resume.

    Used by:
      - Anti-sycophancy eval tests (tests/persona/test_sycophancy.py) —
        fires SycEval / ELEPHANT prompts at a fresh persona session and
        scores the response via Haiku.
      - drift_canary weekly hard-opinion probe (agents.drift_canary).

    Differs from ``run_user_turn`` / ``run_visible_proactive`` in three ways:
      - No session resume — every call is a fresh conversation.
      - No write-back to ``messages``. Probe answers must not pollute the
        chat history.
      - No shared ``_RUN_LOCK`` — these calls never resume the live session,
        so they cannot race with user turns or proactive jobs.

    The full persona + MCP servers + hooks are kept so the response is
    representative of how Hikari actually talks today.
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

    Routes to the aux model (openrouter deepseek or haiku subscription) —
    NOT Sonnet — so reflection is cheap. Neutral system prompt, no MCP
    servers, no hooks, no session resume.
    """
    return await _call_aux_llm(prompt)
