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
import uuid
from contextvars import ContextVar
from functools import cache
from pathlib import Path

import httpx

from claude_agent_sdk import (
    AssistantMessage,
    CLIConnectionError,
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

# ---------- turn_id ContextVar ----------

# Set at run_user_turn/run_visible_proactive entry so all log records emitted
# during a turn are tagged with [turn_id]. Other modules read this via
# current_turn_id(). Lives in this module (not _turn_state) because it's an
# execution-flow concern, not a tool-fabrication concern.
_CURRENT_TURN_ID: ContextVar[str | None] = ContextVar(
    "hikari_current_turn_id", default=None
)


def current_turn_id() -> str | None:
    """Return the active turn ID hex string, or None outside a user turn."""
    return _CURRENT_TURN_ID.get()


class _TurnIdFilter(logging.Filter):
    """Prepend ``[turn_id]`` to every log record emitted during a turn."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        tid = _CURRENT_TURN_ID.get()
        if tid:
            record.msg = f"[{tid}] {record.msg}"
        return True


_turn_id_filter = _TurnIdFilter()

# Attach to the root logger once so all hikari loggers inherit it.
# Idempotent across importlib.reload(agents.runtime) calls used by tests —
# otherwise reloads accumulate filters and double/triple-prefix every record.
_root_logger = logging.getLogger()
if not any(isinstance(f, _TurnIdFilter) for f in _root_logger.filters):
    _root_logger.addFilter(_turn_id_filter)

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

# Fallback must differ from MODEL_PRIMARY (SDK refuses identical model+fallback).
# Use the prior Sonnet release — still non-haiku, satisfies the user rule, and
# is a valid older-but-stable fallback when sonnet-4-6 is unavailable.
_SONNET_DEFAULT_FALLBACK = "claude-sonnet-4-5"


def _resolve_model_fallback() -> str:
    """Resolve MODEL_FALLBACK, enforcing the no-Haiku rule.

    User rule: never Haiku anywhere. If env or config specifies a Haiku
    model ID, override to Sonnet and log a warning — the misconfiguration
    is visible in logs but does not crash import. The correct fix is to
    update config/engagement.yaml:runtime.model_fallback to a Sonnet ID.

    Environment variable wins over config wins over hardcoded default.
    """
    raw = (
        os.environ.get("HIKARI_MODEL_FALLBACK")
        or cfg.get("runtime.model_fallback")
        or _SONNET_DEFAULT_FALLBACK
    )
    raw_str = str(raw)
    if "haiku" in raw_str.lower():
        logging.getLogger("agents.runtime").warning(
            "runtime.model_fallback=%r contains 'haiku' — forbidden by user rule. "
            "Overriding to %s. Fix: update config/engagement.yaml or set HIKARI_MODEL_FALLBACK.",
            raw_str, _SONNET_DEFAULT_FALLBACK,
        )
        return _SONNET_DEFAULT_FALLBACK
    if raw_str == MODEL_PRIMARY:
        override = _SONNET_DEFAULT_FALLBACK if _SONNET_DEFAULT_FALLBACK != MODEL_PRIMARY else "claude-sonnet-4-5"
        logging.getLogger("agents.runtime").warning(
            "runtime.model_fallback=%r equals model_primary — SDK refuses identical model+fallback. "
            "Overriding to %s. Fix: update config/engagement.yaml:runtime.model_fallback.",
            raw_str, override,
        )
        return override
    return raw_str


# Fallback is Sonnet (never Haiku) — user rule: no Haiku anywhere.
MODEL_FALLBACK = _resolve_model_fallback()

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
    """SDK model ID to use for internal-control / SDK-based reflection calls.

    When provider is ``openrouter``, the caller should route through
    ``_call_aux_llm`` (httpx path) rather than this function. Raises
    ``ValueError`` for unknown providers so misconfiguration is never silent.

    User rule: never Haiku. Any provider that would have returned a Haiku
    model ID is a misconfiguration and will raise.
    """
    provider = _aux_provider()
    if provider == "haiku_subscription":
        # haiku_subscription is a legacy config value — refuse per user rule.
        raise ValueError(
            "aux_model.provider='haiku_subscription' is forbidden (no Haiku). "
            "Set provider=openrouter to use DeepSeek via OpenRouter."
        )
    if provider == "openrouter":
        # Not SDK-compatible — callers that need SDK tools should use
        # MODEL_PRIMARY (Sonnet) directly; non-tool calls go via _call_aux_llm.
        raise ValueError(
            "aux_model.provider='openrouter' cannot use the SDK path. "
            "Call _call_aux_llm() instead for non-tool aux operations."
        )
    raise ValueError(
        f"Unknown aux_model.provider={provider!r}. "
        "Valid values: 'openrouter'."
    )


async def _call_aux_llm(
    prompt: str,
    *,
    system: str = _AUX_REFLECTION_SYSTEM,
    model: str | None = None,
    max_tokens: int = 512,
) -> str:
    """Cheap LLM call via OpenRouter for reflection and other no-tool ops.

    All auxiliary (non-persona-turn) LLM work routes here: evening_diary,
    future_letter, reflection consolidation, topic tagging, etc. Uses httpx
    directly against the OpenRouter API — no SDK subprocess, no Haiku.

    Args:
        prompt: User-turn content. The system prompt is fixed structured-output
            instructions (``_AUX_REFLECTION_SYSTEM``) unless overridden.
        system: Override system prompt when the caller has its own instructions.
        model: OpenRouter model ID. Defaults to ``_aux_model_id()`` (reads
            ``aux_model.model`` from config, defaulting to
            ``deepseek/deepseek-v4-flash``).
        max_tokens: Hard cap on completion tokens. Default 512 is a safety cap
            for structured-output tasks (YAML/JSON); pass a higher value only
            for long-form composition (diary, future_letter). Hardcoded as a
            safety cap — this is intentional and documented here.

    Raises:
        RuntimeError: when OPENROUTER_API_KEY is unset (misconfiguration).
        httpx.HTTPStatusError / httpx.RequestError: on non-retried failures.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "_call_aux_llm requires OPENROUTER_API_KEY to be set. "
            "DeepSeek / OpenRouter is the mandatory aux-LLM path (no Haiku fallback)."
        )

    effective_model = model or _aux_model_id()
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]

    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=60.0) as _client:
                resp = await _client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": effective_model,
                        "messages": messages,
                        "max_tokens": max_tokens,
                    },
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

        payload = resp.json()
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError(
                f"aux_llm: unexpected OpenRouter response shape — "
                f"'choices' missing or empty. Full response: {payload!r:.400}"
            )
        message = choices[0].get("message")
        if not isinstance(message, dict) or "content" not in message:
            raise RuntimeError(
                f"aux_llm: unexpected OpenRouter response shape — "
                f"choices[0].message missing or has no 'content'. "
                f"choices[0]={choices[0]!r:.400}"
            )
        return str(message["content"]).strip()

    # Should never reach here (loop always returns or raises), but keep the
    # compiler happy and make the invariant explicit.
    raise RuntimeError("aux_llm: retry loop exhausted without returning")


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
        else:
            logger.warning(
                "notion token not loaded from keychain — re-grant via "
                "`uv run python -m scripts.auth notion grant`"
            )
    except Exception:
        logger.warning(
            "notion token not loaded from keychain — re-grant via "
            "`uv run python -m scripts.auth notion grant`"
        )

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
# run_user_turn / respond defaults. Surfaced into the per-turn ``# now``
# block via agents.hooks._format_now (not the cached persona), so Hikari
# always sees the current value without busting the prompt cache.
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
    """Return the CLAUDE.md text for use as the SDK system prompt.

    Cached once per process. Per-turn values (max_turns, time, etc.) live in
    the ``# now`` block injected by ``agents.hooks._format_now`` — never
    substituted here, since per-turn substitution would defeat the Anthropic
    prompt cache.
    """
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
    reconnects once, retries once, then re-raises on second failure. On
    CLIConnectionError, reconnects without clearing session_id: the cached
    subprocess is dead, but the SDK session itself is not necessarily suspect.
    On ResultMessage: stores session_id if log_session_id=True.

    Lock contract: callers (``run_user_turn``, ``run_visible_proactive``,
    ``run_user_turn_blocks``) MUST hold ``_RUN_LOCK`` before calling this
    function. ``_reconnect_live(lock_run=False)`` is safe here because the
    outer lock is already held — do NOT pass ``lock_run=True`` from inside
    the lock or you will deadlock.
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
    except (TimeoutError, ProcessError, CLIConnectionError) as exc:
        reason = type(exc).__name__
        # CLIConnectionError means the cached CLI transport died. Reconnect the
        # client, but keep session_id so the retry can resume the live chat.
        if isinstance(exc, CLIConnectionError):
            logger.warning(
                "_invoke_sdk_persistent_live: %s — reconnecting dead live client",
                reason,
            )
        else:
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

    Sets ``_CURRENT_TURN_ID`` at entry so all log records for this turn are
    prefixed with ``[turn_id]``.
    """
    _CURRENT_TURN_ID.set(uuid.uuid4().hex)
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

    Sets ``_CURRENT_TURN_ID`` at entry for log correlation.
    """
    _CURRENT_TURN_ID.set(uuid.uuid4().hex)
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

    Sets ``_CURRENT_TURN_ID`` at entry for log correlation (proactive turns
    are indistinguishable from user turns in the lock contract).
    """
    _CURRENT_TURN_ID.set(uuid.uuid4().hex)
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

    Uses ``MODEL_PRIMARY`` (Sonnet) — internal control prompts may invoke SDK
    tools (approval resume, GCal sync, etc.) and must use the subscription
    model. Never Haiku.
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
        model=MODEL_PRIMARY,
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
        scores the response via DeepSeek (``run_reflection_call``).
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

    Routes via ``_call_aux_llm`` → OpenRouter → DeepSeek V4 Flash. Cost is
    ~$0.14/$0.28 per 1M tokens, which is orders of magnitude cheaper than
    Sonnet and sufficient for YAML extraction / topic tagging / consolidation.

    No MCP servers, no hooks, no session resume. Any callers that were
    previously using Haiku or the Claude SDK for reflection must use this
    function. Token cap is 2048 to accommodate longer YAML reflection outputs
    (daily reflection prompt + entity blocks can produce 800-1200 tokens).
    """
    return await _call_aux_llm(prompt, max_tokens=2048)


async def run_aux_composition(
    prompt: str,
    *,
    system: str | None = None,
    max_tokens: int = 1024,
) -> str:
    """Cheap LLM call for private text-generation tasks: diary, future_letter,
    and any other composition that does NOT need SDK tools or session context.

    Routes via ``_call_aux_llm`` → OpenRouter → DeepSeek V4 Flash.
    Unlike ``run_internal_control``, this path never spawns an SDK subprocess
    — it's pure httpx, so there is no per-turn budget, no process fork, and
    no risk of leaking into the live session. Use for:

    - ``evening_diary.compose_diary`` — private diary composition
    - ``future_letter.pick_decision_theme`` / ``compose_letter`` — monthly letter
    - Any future private-generation task that needs >512 tokens but no tools

    Token default is 1024 (composition tasks produce longer output than YAML
    classifiers but shorter than weekly consolidation). Pass a higher value
    (e.g. 2048) for long-form letter bodies.
    """
    kwargs: dict = {"max_tokens": max_tokens}
    if system is not None:
        kwargs["system"] = system
    return await _call_aux_llm(prompt, **kwargs)
