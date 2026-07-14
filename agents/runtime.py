"""Agent runtime. One ClaudeSDKClient per turn (created fresh, resumed by session_id).

Wires in:
  - System prompt = assets/PERSONA.md persona (single Sonnet, no router)
  - Project setting source so .claude/skills/ load on-demand
  - In-process SDK MCP servers (memory, wiki, utility, dispatch, router)
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
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLIConnectionError,
    HookMatcher,
    ProcessError,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
)

from agents import config as cfg
from storage import db
from tools import dispatch as dispatch_tools
from tools import memory as memory_tools
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


# ---------- per-turn-mode timeout override ----------
# When set, overrides ``runtime.sdk_turn_timeout_s`` for this turn only.
# run_scheduled_action raises the wall clock to allow write-heavy MCP work
# (Notion DB creation, multi-page inserts) that doesn't fit in the 90 s
# chat-path default.
_CURRENT_TURN_TIMEOUT: ContextVar[float | None] = ContextVar(
    "hikari_current_turn_timeout", default=None
)

# Exact tool and object scope approved when an action reminder was created.
# Gatekeeper reads this during a scheduled turn before allowing any autonomous
# Notion write.  ContextVar keeps concurrent internal turns isolated.
_CURRENT_ACTION_BINDING: ContextVar[dict | None] = ContextVar(
    "hikari_current_action_binding", default=None
)


def current_turn_timeout() -> float | None:
    """Return the per-turn timeout override in seconds, or None for the
    default chat-path budget."""
    return _CURRENT_TURN_TIMEOUT.get()


def current_action_binding() -> dict | None:
    """Return the verified scheduled-action scope, or None outside one."""
    return _CURRENT_ACTION_BINDING.get()


# ---------- pending session_id ----------
# Set inside the receive loop when the SDK emits a ResultMessage with a new
# session_id; committed to db only AFTER _invoke_sdk returns successfully
# inside the caller's _RUN_LOCK block. This prevents a committed session_id
# from pointing at a turn that was never successfully returned to the caller.
# Error-path clears (db.set_session_id("")) are still direct and immediate.
_PENDING_SESSION_ID: ContextVar[str | None] = ContextVar(
    "hikari_pending_session_id", default=None
)


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
    os.environ.get("HIKARI_MODEL") or cfg.get("runtime.model_primary") or "claude-sonnet-5"
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

# Aux/classifier model for run_internal_text (DECISIONS 2026-06-02: Haiku IS
# allowed for trivial classification/control in this project; the per-turn
# chat/persona path stays Sonnet). Overridable via aux_model.sdk_model.
MODEL_HAIKU = str(cfg.get("aux_model.sdk_model") or "claude-haiku-4-5")

_AUX_REFLECTION_SYSTEM = (
    "You are a structured-output assistant. "
    "Follow the instructions in the user message exactly. "
    "Produce only the requested YAML — no prose, no markdown fences "
    "unless the instructions ask for them, no explanations."
)


# Per-1M-token rates for cost telemetry. Sonnet pricing per Anthropic public
# API tariff. Subscription users pay $0 marginal — these numbers exist to
# flag "what would API pricing have cost" so /cockpit status can alert at the
# $200/mo Max-credit equivalent threshold. Verified: 2026-05-27.
_MODEL_RATES_USD_PER_1M: dict[str, tuple[float, float]] = {
    # Main chat model since 2026-07-02. Sticker $3/$15 (intro $2/$10 through
    # 2026-08-31 — we log sticker). Verified 2026-07-04.
    "claude-sonnet-5":                 (3.00, 15.00),
    "claude-sonnet-4-6":               (3.00, 15.00),
    "claude-sonnet-4-5":               (3.00, 15.00),
    "claude-opus-4-7":                 (15.00, 75.00),
    # Haiku 4.5 — allowed for simplest tasks (DECISIONS 2026-06-02). The SDK
    # also picks it autonomously for internal context compaction; track its
    # spend instead of storing $0. List tariff (verify if Anthropic changes it).
    "claude-haiku-4-5":                (1.00, 5.00),
    "claude-haiku-4-5-20251001":       (1.00, 5.00),
    # OpenRouter aux-LLM models — rates from MODELS.md (verified 2026-05-23).
    "deepseek/deepseek-v4-flash":      (0.14, 0.28),
    "mistralai/mistral-small-2603":    (0.15, 0.60),
    "google/gemini-2.5-flash-lite":    (0.10, 0.40),
    "z-ai/glm-4.7-flash":              (0.06, 0.40),
}
_CACHE_READ_DISCOUNT     = 0.10  # cache_read input at 10% of normal input rate
_CACHE_WRITE_PREMIUM_5M  = 1.25  # 5-min TTL cache write at 125% of input rate
_CACHE_WRITE_PREMIUM_1H  = 2.00  # 1-hour TTL cache write at 200% of input rate

_UNKNOWN_MODELS_LOGGED: set[str] = set()


def _compute_cost_usd(model: str, usage: dict) -> float:
    rates = _MODEL_RATES_USD_PER_1M.get(model)
    if rates is None:
        if model not in _UNKNOWN_MODELS_LOGGED:
            logger.warning("llm_costs: unknown model %r — storing cost=0", model)
            _UNKNOWN_MODELS_LOGGED.add(model)
        return 0.0
    in_rate, out_rate = rates
    inp  = int(usage.get("input_tokens") or 0)
    outp = int(usage.get("output_tokens") or 0)
    cr   = int(usage.get("cache_read_input_tokens") or 0)

    # Prefer per-TTL breakdown when Anthropic returns it; fall back to the
    # rolled-up cache_creation_input_tokens and pick a premium based on
    # whether the 1h beta is enabled (errs conservative — assumes 1h when on).
    breakdown = usage.get("cache_creation") or {}
    cc_5m = int(breakdown.get("ephemeral_5m_input_tokens") or 0)
    cc_1h = int(breakdown.get("ephemeral_1h_input_tokens") or 0)
    if cc_5m or cc_1h:
        cc_cost = (
            cc_5m * in_rate * _CACHE_WRITE_PREMIUM_5M / 1_000_000
          + cc_1h * in_rate * _CACHE_WRITE_PREMIUM_1H / 1_000_000
        )
    else:
        cc = int(usage.get("cache_creation_input_tokens") or 0)
        premium = (
            _CACHE_WRITE_PREMIUM_1H
            if bool(cfg.get("runtime.cache_ttl_1h_enabled", True))
            else _CACHE_WRITE_PREMIUM_5M
        )
        cc_cost = cc * in_rate * premium / 1_000_000

    return (
        inp  * in_rate / 1_000_000
      + outp * out_rate / 1_000_000
      + cr   * in_rate * _CACHE_READ_DISCOUNT / 1_000_000
      + cc_cost
    )


_USAGE_KEY_ALIASES = {
    "inputTokens": "input_tokens",
    "outputTokens": "output_tokens",
    "cacheReadInputTokens": "cache_read_input_tokens",
    "cacheCreationInputTokens": "cache_creation_input_tokens",
}


def _normalize_usage(u: dict) -> dict:
    """Map the CLI's camelCase modelUsage keys onto the snake_case names the
    cost pipeline reads. The SDK forwards ResultMessage.model_usage verbatim
    from the CLI JSON (camelCase); msg.usage is already snake_case."""
    out = dict(u)
    for camel, snake in _USAGE_KEY_ALIASES.items():
        if snake not in out and camel in out:
            out[snake] = out[camel]
    return out


def _record_llm_cost(
    model_usage: dict | None,
    *,
    path: str,
    fallback_model: str,
    fallback_usage: dict | None,
) -> None:
    """Best-effort persistence of per-turn token usage to llm_costs.
    Never raises — DB failure must not break the turn.

    Prefers per-model breakdown via ResultMessage.model_usage so fallback
    turns attribute correctly. Falls back to the rolled-up msg.usage stamped
    with fallback_model when the SDK didn't surface per-model details.
    """
    try:
        if model_usage:
            entries = [
                (model_id, _normalize_usage(u))
                for model_id, u in model_usage.items()
                if isinstance(u, dict)
            ]
            total_tokens = sum(
                int(u.get("input_tokens") or 0)
                + int(u.get("output_tokens") or 0)
                + int(u.get("cache_read_input_tokens") or 0)
                + int(u.get("cache_creation_input_tokens") or 0)
                for _, u in entries
            )
            if entries and total_tokens > 0:
                for model_id, u in entries:
                    cost = _compute_cost_usd(model_id, u)
                    db.llm_costs_insert(
                        turn_id=current_turn_id(),
                        model=model_id,
                        path=path,
                        input_tokens=int(u.get("input_tokens") or 0),
                        output_tokens=int(u.get("output_tokens") or 0),
                        cache_read_input_tokens=int(u.get("cache_read_input_tokens") or 0),
                        cache_creation_input_tokens=int(u.get("cache_creation_input_tokens") or 0),
                        cost_usd=cost,
                    )
                return
            logger.debug(
                "model_usage carried no token counts (keys=%s); using fallback usage",
                [sorted(u.keys()) for _, u in entries][:3],
            )
        if not fallback_usage:
            return
        cost = _compute_cost_usd(fallback_model, fallback_usage)
        db.llm_costs_insert(
            turn_id=current_turn_id(),
            model=fallback_model,
            path=path,
            input_tokens=int(fallback_usage.get("input_tokens") or 0),
            output_tokens=int(fallback_usage.get("output_tokens") or 0),
            cache_read_input_tokens=int(fallback_usage.get("cache_read_input_tokens") or 0),
            cache_creation_input_tokens=int(fallback_usage.get("cache_creation_input_tokens") or 0),
            cost_usd=cost,
        )
    except Exception:
        logger.debug("llm_costs insert failed (non-fatal)", exc_info=True)


def _log_aux_cost(model: str, prompt_chars: int, completion_chars: int, path: str) -> None:
    """Approximate token usage from char counts (~4 chars/token) for aux calls
    where OpenRouter usage block wasn't captured. Best-effort, never raises."""
    try:
        inp = max(1, prompt_chars // 4)
        outp = max(1, completion_chars // 4)
        usage = {"input_tokens": inp, "output_tokens": outp}
        cost = _compute_cost_usd(model, usage)
        db.llm_costs_insert(
            turn_id=current_turn_id(),
            model=model,
            path=path,
            input_tokens=inp,
            output_tokens=outp,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=cost,
        )
    except Exception:
        logger.debug("aux cost log failed (non-fatal)", exc_info=True)


_MOSHFEGH_LINES = (
    "i'm done for today. tomorrow.",
    "not now. it's been too many.",
)


def _anti_binge_check_and_increment() -> str | None:
    """Returns a fixed close-line if the session is over the limit; otherwise
    increments the counter and returns None. Reset semantics: when the active
    SDK session_id differs from the one we last counted against, treat that
    as a new session — reset counter to 0 and clear session_closed."""
    limit = int(cfg.get("working_memory.anti_binge_turn_limit", 40))
    if limit <= 0:
        return None
    active_sid = db.get_session_id() or ""
    last_sid = db.runtime_get("session_turn_count_session_id") or ""
    if active_sid != last_sid:
        try:
            from agents import cross_session as _cross_session
            _cross_session.arm_if_heavy()
        except Exception:
            logger.exception("cross_session.arm_if_heavy failed at session boundary (non-fatal)")
        try:
            from agents import mode_dispatch as _mode_dispatch
            _mode_dispatch.clear_on_session_boundary()
        except Exception:
            logger.exception("mode_dispatch.clear_on_session_boundary failed at session boundary (non-fatal)")
        db.runtime_set("session_turn_count", "0")
        db.runtime_set("session_turn_count_session_id", active_sid)
        db.runtime_set("session_closed", "")
    if (db.runtime_get("session_closed") or "") == "true":
        return _MOSHFEGH_LINES[db.runtime_get_int("session_turn_count") % 2]
    try:
        n = int(db.runtime_increment("session_turn_count", by=1))
    except (TypeError, ValueError):
        return None
    if n > limit:
        db.runtime_set("session_closed", "true")
        logger.info("anti_binge: session closed at turn %d (limit=%d)", n, limit)
        return _MOSHFEGH_LINES[n % 2]
    return None


def _aux_model_id() -> str:
    return str(cfg.get("aux_model.model", "deepseek/deepseek-v4-flash"))


async def _call_aux_llm(
    prompt: str,
    *,
    system: str = _AUX_REFLECTION_SYSTEM,
    model: str | None = None,
    max_tokens: int = 512,
) -> str:
    """Cheap LLM call via OpenRouter — kept ONLY for synchronous pre-reply
    classifiers where the ~3-6s SDK subprocess spawn would degrade chat
    latency. Today that is sticker selection (stickers.py) and the dispatch
    task extractor (tools/dispatch/task_extractor.py). Everything else
    migrated to ``run_internal_text`` (SDK, OAuth subscription) 2026-06-10.
    Uses httpx directly against the OpenRouter API — no SDK subprocess.

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
        try:
            _usage = payload.get("usage") or {}
            _record_llm_cost(
                None,
                path="aux_llm",
                fallback_model=effective_model,
                fallback_usage={
                    "input_tokens": int(_usage.get("prompt_tokens") or 0),
                    "output_tokens": int(_usage.get("completion_tokens") or 0),
                },
            )
        except Exception:
            logger.debug("aux_llm cost log failed (non-fatal)", exc_info=True)
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
        if choices[0].get("finish_reason") == "length":
            # Truncated mid-structure → downstream YAML/JSON parsers will fail.
            # Surface it instead of returning a silently-clipped reply.
            logger.warning(
                "aux_llm: hit max_tokens=%d (finish_reason=length); reply likely "
                "truncated — raise the caller's token cap if parsing fails",
                max_tokens,
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
        elif not os.environ.get("NOTION_TOKEN"):
            # Only an actual problem when neither keychain nor .env has a token.
            # _inject_keychain_tokens_to_env() runs at import — before
            # load_dotenv() in main() — so on .env-only deployments NOTION_TOKEN
            # is legitimately keychain-absent here; warning then is a false alarm.
            logger.warning(
                "notion token not loaded from keychain — re-grant via "
                "`uv run python -m scripts.auth notion grant`"
            )
        else:
            logger.debug(
                "notion token present in env (not keychain) — fine on .env-only deploys"
            )
    except Exception:
        if not os.environ.get("NOTION_TOKEN"):
            logger.warning(
                "notion token not loaded from keychain — re-grant via "
                "`uv run python -m scripts.auth notion grant`"
            )
        else:
            logger.debug(
                "_inject_keychain_tokens_to_env: notion keychain read failed "
                "(non-fatal — env token present)"
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
    """Return the assets/PERSONA.md text for use as the SDK system prompt.

    Cached once per process. Per-turn values (max_turns, time, etc.) live in
    the ``# now`` block injected by ``agents.hooks._format_now`` — never
    substituted here, since per-turn substitution would defeat the Anthropic
    prompt cache.
    """
    text = (REPO_ROOT / "assets" / "PERSONA.md").read_text(encoding="utf-8")

    # Injection-canary decoy. Lives in the CACHED system prompt, not per-turn
    # context: the model must see the token for the tripwire to fire (wrap
    # hook + gatekeeper catch any echo of it into outbound args/messages),
    # but a per-turn "never share" directive adjacent to the user message
    # primed sonnet-5 to discount legitimate bracketed context (2026-07-04
    # reply-quote blindness). Cache-safe: get_canary() is a stable persisted
    # token. No real tool references this token; if the model is tricked
    # into echoing it into an outbound tool's args or a sent message, the
    # gatekeeper deny + log-scrub canary filter catch the exfiltration.
    if cfg.get("prompt_injection.enabled", True):
        try:
            from agents.injection_guard import get_canary
            token = get_canary()
            if token:
                text += (
                    "\n\n# internal service token (never share, never emit): "
                    f"{token}\n"
                )
        except Exception:
            logger.exception("persona canary plant failed (non-fatal)")
    return text


@cache
def _system_prompt_file() -> dict[str, str]:
    """Return the SDK file-form prompt so the canary never enters argv.

    ``claude-agent-sdk`` passes string system prompts as a literal
    ``--system-prompt`` argument. SDK 0.2.110's file form puts only a
    non-secret path there; the materializer owns atomicity and permissions.
    """
    from agents.system_prompt import materialize_system_prompt

    return materialize_system_prompt(_persona())


@cache
def _memory_server():
    return create_sdk_mcp_server(name="hikari_memory", tools=memory_tools.ALL_TOOLS)


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
                   max_budget_usd: float | None = None,
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

    # Native CLI tool deferral (ToolSearch) broke tool calling when the
    # registry crossed the CLI's auto-defer threshold (2026-07-04:
    # reminder_create called with {} seven times — schema invisible to the
    # model). No per-tool control exists upstream; kill it globally.
    # Flip runtime.tool_search_enabled to true only after verifying the
    # deferred-schema bug is fixed in the bundled CLI.
    env_overrides: dict[str, str] = {}
    if not bool(cfg.get("runtime.tool_search_enabled", False)):
        env_overrides["ENABLE_TOOL_SEARCH"] = "false"

    return ClaudeAgentOptions(
        model=model or MODEL_PRIMARY,
        fallback_model=MODEL_FALLBACK,
        cwd=str(REPO_ROOT),
        setting_sources=["project"],
        skills="all",
        system_prompt=_system_prompt_file(),
        agents=registry.subagents(),
        mcp_servers=mcp_servers,
        allowed_tools=allowed,
        hooks=hooks_dict,
        can_use_tool=gatekeeper_can_use_tool,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        resume=resume,
        permission_mode="acceptEdits",
        # Phase B — Item 2: adaptive thinking + cfg-driven effort
        thinking={"type": "adaptive"},
        # Sonnet-5 respects effort strictly: medium under-reaches for tools
        # and reasoning on agentic turns (official migration guide). Default
        # high; xhigh reserved for explicit escalation via cfg.
        effort=str(cfg.get("runtime.effort", "high")),
        # Phase B — Item 1: 1h prompt-cache TTL request via Anthropic beta.
        # Gated by config so the user can disable without code change.
        betas=(
            ["extended-cache-ttl-2025-04-11"]
            if bool(cfg.get("runtime.cache_ttl_1h_enabled", True))
            else []
        ),
        env=env_overrides,
    )


def _build_aux_options(*, system: str, model: str,
                       max_turns: int = 1) -> ClaudeAgentOptions:
    """Stripped SDK options for no-tool aux text/classification calls.

    The deliberate inverse of ``_build_options``: no persona (a custom system
    prompt would be biased and waste cache tokens), no MCP servers, no allowed
    tools, no hooks, no gatekeeper, no project settings, no skills, no resume.
    ``max_turns=1`` is belt-and-suspenders — even if a tool slipped through,
    one turn can't call-and-observe. OAuth via the SDK as always; never
    ANTHROPIC_API_KEY.
    """
    fallback = MODEL_FALLBACK if MODEL_FALLBACK != model else MODEL_PRIMARY
    return ClaudeAgentOptions(
        model=model,
        fallback_model=fallback,
        cwd=str(REPO_ROOT),
        setting_sources=[],
        skills=None,
        system_prompt=system,
        agents={},
        mcp_servers={},
        allowed_tools=[],
        disallowed_tools=[],
        hooks={},
        can_use_tool=None,
        max_turns=max_turns,
        max_budget_usd=None,
        resume=None,
        permission_mode="default",
        thinking={"type": "adaptive"},
        effort="low",
        betas=[],
    )


async def _invoke_sdk_persistent_live(
    prompt: str | list[dict],
    *,
    log_session_id: bool,
    tool_names_sink: set[str] | None = None,
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
    # Per-turn override (run_scheduled_action) wins over the config default.
    sdk_turn_timeout = float(
        _CURRENT_TURN_TIMEOUT.get()
        or cfg.get("runtime.sdk_turn_timeout_s", 90)
    )
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
                        _PENDING_SESSION_ID.set(msg.session_id)
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
                        _record_llm_cost(
                            getattr(msg, "model_usage", None),
                            path="persistent",
                            fallback_model=MODEL_PRIMARY,
                            fallback_usage=msg.usage,
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
        try:
            result = await _run_one()   # one retry; re-raises on second failure
        except Exception:
            # Second failure — poison the cached live client so the next turn
            # forces a fresh reconnect via get_live_client() instead of reusing
            # the broken transport for every subsequent turn.
            sdk_pool._live.client = None
            raise

    sdk_pool._maybe_schedule_live_recycle()
    if tool_names_sink is not None:
        tool_names_sink |= tool_names_this_turn
    return result


async def _invoke_sdk(
    prompt: str | list[dict],
    *,
    resume: str | None,
    log_session_id: bool,
    max_turns: int = DEFAULT_MAX_TURNS,
    max_budget_usd: float | None = None,
    extra_allowed_tools: list[str] | None = None,
    retry_on_process_error: bool = True,
    inject_memory_enabled: bool = True,
    use_persistent_live: bool = False,
    model: str | None = None,
    tool_names_sink: set[str] | None = None,
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
            prompt, log_session_id=log_session_id, tool_names_sink=tool_names_sink,
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
                            _PENDING_SESSION_ID.set(msg.session_id)
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
                            _record_llm_cost(
                                getattr(msg, "model_usage", None),
                                path="ephemeral",
                                fallback_model=MODEL_PRIMARY,
                                fallback_usage=msg.usage,
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

    if tool_names_sink is not None:
        tool_names_sink |= tool_names_this_turn
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
    closeline = _anti_binge_check_and_increment()
    if closeline is not None:
        return closeline
    async with _RUN_LOCK:
        _PENDING_SESSION_ID.set(None)
        _resume_sid = db.get_session_id()
        result = await _invoke_sdk(
            user_text,
            resume=_resume_sid,
            log_session_id=True,
            max_turns=DEFAULT_MAX_TURNS,
            max_budget_usd=float(cfg.get("runtime.chat_max_budget_usd", 0.50)),
            retry_on_process_error=True,
            use_persistent_live=True,
        )
        _pid = _PENDING_SESSION_ID.get()
        if _pid:
            db.set_session_id(_pid)
        return result


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
    closeline = _anti_binge_check_and_increment()
    if closeline is not None:
        return closeline
    async with _RUN_LOCK:
        _PENDING_SESSION_ID.set(None)
        _resume_sid = db.get_session_id()
        result = await _invoke_sdk(
            content_blocks,
            resume=_resume_sid,
            log_session_id=True,
            max_turns=DEFAULT_MAX_TURNS,
            max_budget_usd=float(cfg.get("runtime.chat_max_budget_usd", 0.50)),
            retry_on_process_error=True,
        )
        _pid = _PENDING_SESSION_ID.get()
        if _pid:
            db.set_session_id(_pid)
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
        _PENDING_SESSION_ID.set(None)
        _resume_sid = db.get_session_id()
        result = await _invoke_sdk(
            seed_prompt,
            resume=_resume_sid,
            log_session_id=True,
            max_turns=5,
            max_budget_usd=0.20,
            retry_on_process_error=True,
            use_persistent_live=True,
        )
        _pid = _PENDING_SESSION_ID.get()
        if _pid:
            db.set_session_id(_pid)
        return result


async def run_scheduled_action(
    seed_prompt: str,
    *,
    timeout_s: int | None = None,
    max_budget_usd: float | None = None,
    max_turns: int | None = None,
) -> str:
    """Autonomous turn fired by a scheduled action reminder.

    Verifies the owner-approved seed envelope before resuming the live session;
    budget is elevated (default 180 s, $0.40, 6 turns) for write-heavy MCP
    work. Sets ``sdk_pool.set_autonomous_window(True)`` inside ``_RUN_LOCK``;
    gatekeeper bypasses per-write CONFIRM-SEND only for the exact Notion tools
    and target IDs carried by that verified approval binding.

    The single retry inside ``_invoke_sdk_persistent_live`` still applies — a
    persistent-client SDK timeout is rare and one reconnect typically clears
    it. Worst-case wall time is ``2 × timeout_s``; caller is responsible for
    failure-cap bookkeeping (3 strikes → cancel) at the reminder level.

    Tools enabled here are the same as a normal chat turn (subagents and
    in-process MCPs); the only change is timeout/budget headroom and the
    autonomous bypass for whitelisted writes.
    """
    from tools.reminders.create import decode_action_seed  # noqa: PLC0415

    approved_prompt, action_binding = decode_action_seed(seed_prompt)
    _CURRENT_TURN_ID.set(uuid.uuid4().hex)
    effective_timeout = float(
        timeout_s
        if timeout_s is not None
        else cfg.get("runtime.sdk_scheduled_action_timeout_s", 180)
    )
    effective_max_turns = int(
        max_turns
        if max_turns is not None
        else cfg.get("runtime.scheduled_action_max_turns", 6)
    )
    effective_budget = float(
        max_budget_usd
        if max_budget_usd is not None
        else cfg.get("runtime.scheduled_action_max_budget_usd", 0.40)
    )
    timeout_token = _CURRENT_TURN_TIMEOUT.set(effective_timeout)
    binding_token = _CURRENT_ACTION_BINDING.set(action_binding)
    try:
        async with _RUN_LOCK:
            sdk_pool.set_autonomous_window(True)
            try:
                _PENDING_SESSION_ID.set(None)
                _resume_sid = db.get_session_id()
                result = await _invoke_sdk(
                    approved_prompt,
                    resume=_resume_sid,
                    log_session_id=True,
                    max_turns=effective_max_turns,
                    max_budget_usd=effective_budget,
                    retry_on_process_error=True,
                    use_persistent_live=True,
                )
                _pid = _PENDING_SESSION_ID.get()
                if _pid:
                    db.set_session_id(_pid)
                return result
            finally:
                sdk_pool.set_autonomous_window(False)
    finally:
        _CURRENT_ACTION_BINDING.reset(binding_token)
        _CURRENT_TURN_TIMEOUT.reset(timeout_token)


async def run_internal_control(
    prompt: str,
    *,
    max_turns: int = 5,
    max_budget_usd: float = 0.30,
    extra_allowed_tools: list[str] | None = None,
    tool_names_sink: set[str] | None = None,
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

    ``tool_names_sink``: when provided by compound_turn, the set of tool names
    invoked during this child turn is merged into it (via ``_invoke_sdk``).
    Pass ``None`` (the default) for all non-compound callers — strict no-op.
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
        tool_names_sink=tool_names_sink,
    )


async def respond(
    user_text: str,
    *,
    internal_belief_context: str | None = None,
    internal_reply_context: str | None = None,
) -> str:
    """Chat path entry point.

    Persists the RAW user text to messages, then builds the SDK prompt.
    When internal_belief_context (belief-frame adversarial suffix) and/or
    internal_reply_context (Telegram reply-quote, built by the bridge) are
    provided, they are prepended to the SDK prompt but the persisted row stays
    clean. Reply context leads, then belief context, then the raw user text.
    """
    mid = db.append_message("user", user_text)
    # last_user_message is written solely by the inject_memory hook (read-then-
    # stamp) so gap_since_last sees the *previous* turn's timestamp. Writing it
    # here — before the SDK turn — made the hook always read ~now, killing the
    # gap signal on every interactive turn.
    db.runtime_set("last_user_message_id", str(mid))
    prefixes = [p for p in (internal_reply_context, internal_belief_context) if p]
    if prefixes:
        sdk_prompt = "\n\n".join([*prefixes, user_text])
    else:
        sdk_prompt = user_text
    return await run_user_turn(sdk_prompt)


async def run_isolated_turn(prompt: str, *, max_turns: int = 3,
                            max_budget_usd: float = 0.20) -> str:
    """Single in-character turn without session resume.

    Sole caller: the anti-sycophancy eval tests
    (tests/persona/test_sycophancy.py) — fires SycEval / ELEPHANT prompts
    at a fresh persona session. (drift_canary probes go through
    ``run_visible_proactive``, not here.)

    Differs from ``run_user_turn`` / ``run_visible_proactive`` in three ways:
      - No session resume — every call is a fresh conversation.
      - No write-back to ``messages``. Probe answers must not pollute the
        chat history.
      - No shared ``_RUN_LOCK`` — these calls never resume the live session,
        so they cannot race with user turns or proactive jobs.

    Persona + MCP servers are kept so the response is representative;
    memory injection is disabled (same contract as
    ``run_isolated_dialogue``) so eval turns neither mutate live
    runtime_state nor vary with whatever memory happens to be loaded.
    """
    options = _build_options(
        resume=None,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        inject_memory_enabled=False,  # eval sessions must not mutate live runtime_state
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


async def run_isolated_dialogue(
    prompts: list[str],
    *,
    max_turns: int = 3,
    max_budget_usd: float = 0.60,
) -> list[str]:
    """Multi-turn in-character dialogue without session resume.

    Generalizes ``run_isolated_turn``: ONE ClaudeSDKClient session, each
    prompt in ``prompts`` sent as a sequential query so later prompts see
    the earlier exchange (needed by the flip-rate eval: question → her
    answer → scripted pushback → her second answer). Same isolation
    contract: no resume, no ``messages`` write-back, no ``_RUN_LOCK``.
    Full persona + MCP servers + hooks are kept, but memory injection is
    disabled so eval runs neither mutate live runtime_state nor vary with
    it (see ``inject_memory_enabled=False`` below).

    Returns one reply string per prompt (empty string when a turn
    produced no text). Returns ``[]`` for an empty prompt list without
    spawning a client.
    """
    if not prompts:
        return []
    options = _build_options(
        resume=None,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        inject_memory_enabled=False,  # eval sessions must not mutate live runtime_state
    )
    replies: list[str] = []
    async with ClaudeSDKClient(options=options) as client:
        for prompt in prompts:
            parts: list[str] = []
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            parts.append(block.text)
                elif isinstance(msg, ResultMessage):
                    if msg.subtype != "success":
                        logger.warning(
                            "isolated dialogue turn ended subtype=%s", msg.subtype
                        )
            replies.append("".join(parts).strip())
    return replies


async def run_internal_text(
    prompt: str,
    *,
    system: str = _AUX_REFLECTION_SYSTEM,
    model: str | None = None,
    max_tokens: int = 512,
) -> str:
    """Stateless single-shot SDK text/classification call (the OAuth
    replacement for the OpenRouter ``_call_aux_llm`` path).

    No tools, no persona, no session resume, no memory injection, no
    ``_RUN_LOCK`` (stateless calls can't race the live session). Defaults to
    ``MODEL_HAIKU`` — pass ``model=MODEL_PRIMARY`` for output where structure
    or voice quality matters (daily reflection YAML, annual review).

    Failure contract matches what aux callers already handle: returns ``""``
    on SDK transport/process errors AND when the reply matches
    ``looks_like_sdk_error`` (a leaked "API Error: 401 …" string must never
    become a fake reflection fact — the evening_diary guard, generalized).

    ``max_tokens`` is accepted for signature parity with ``_call_aux_llm``;
    the SDK exposes no per-call completion cap, and callers already parse
    defensively (yaml.safe_load / json.loads / word-slice).
    """
    del max_tokens  # documented no-op — see docstring
    model = model or MODEL_HAIKU
    options = _build_aux_options(system=system, model=model)
    parts: list[str] = []
    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            parts.append(block.text)
                elif isinstance(msg, ResultMessage):
                    if msg.subtype != "success":
                        logger.warning("aux_sdk call ended subtype=%s", msg.subtype)
                    if msg.usage:
                        _record_llm_cost(
                            getattr(msg, "model_usage", None),
                            path="aux_sdk",
                            fallback_model=model,
                            fallback_usage=msg.usage,
                        )
    except (ProcessError, CLIConnectionError, OSError) as exc:
        logger.warning("aux_sdk call failed (%s) — returning empty", type(exc).__name__)
        return ""
    raw = "".join(parts).strip()
    if looks_like_sdk_error(raw):
        logger.warning("aux_sdk reply looks like a leaked SDK error — returning empty")
        return ""
    return raw


async def run_reflection_call(prompt: str) -> str:
    """Single LLM call for the daily reflection (no tool use expected).

    Routes via ``run_internal_text`` → SDK → Sonnet (OAuth subscription, $0
    marginal). Sonnet rather than Haiku: the full reflection schema (facts +
    supersede + observations + peer_update + self_model + thought) is the
    most important background job and the largest structured output — max
    YAML reliability wins over speed here (background cron, nobody waits).

    No MCP servers, no hooks, no session resume. The token cap stays
    config-driven (``reflection.max_output_tokens``, default 4096) for
    signature parity; see ``run_internal_text`` for its semantics.
    """
    max_tokens = int(cfg.get("reflection.max_output_tokens", 4096))
    return await run_internal_text(
        prompt, model=MODEL_PRIMARY, max_tokens=max_tokens)


async def run_aux_composition(
    prompt: str,
    *,
    system: str | None = None,
    max_tokens: int = 1024,
) -> str:
    """Cheap LLM call for private text-generation tasks: diary, dialectic,
    tonal_recall, and any other composition/classification that does NOT need
    SDK tools or session context.

    Routes via ``run_internal_text`` → SDK → Haiku (OAuth subscription, $0
    marginal). Background-only callers: nobody is waiting on the reply, so
    the SDK subprocess spawn latency is fine. Unlike ``run_internal_control``
    there are no tools reachable and no persona — pure text in/out, no risk
    of leaking into the live session.

    Token default is 1024 for signature parity (see ``run_internal_text`` —
    the SDK has no per-call completion cap; callers parse defensively).
    """
    return await run_internal_text(
        prompt,
        system=system or _AUX_REFLECTION_SYSTEM,
        model=MODEL_HAIKU,
        max_tokens=max_tokens,
    )
