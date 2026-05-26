"""Shared helpers + constants for the dispatch tool.

``dispatch_claude_session`` routes through ``_do_dispatch``. Gating
(CONFIRM-SEND) is handled by the gatekeeper can_use_tool hook, not by
a PreToolUse arg-gate.

Module-level state (``DISPATCH_EVENTS`` queue, ``_OWNER_CHAT_ID``) lives
here. The queue is drained by ``agents/background_listener``; the
chat-id slot is populated by ``agents/telegram_bridge`` at startup via
``set_owner_chat_id``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from storage import db
from tools._response import ok as _ok

logger = logging.getLogger(__name__)

# Module-level queue + chat_id slot. Populated by telegram_bridge at startup.
# (task_id, event_type, payload) tuples. Drained by agents/background_listener.
DISPATCH_EVENTS: asyncio.Queue[tuple[str, str, dict[str, Any]]] = asyncio.Queue()

# Set by telegram_bridge.post_init so the dispatch tool can resolve owner chat.
_OWNER_CHAT_ID: int | None = None

# Root that all dispatched repos must live under. Set HIKARI_WORK_DIR to override;
# default is the parent of this repo (so sibling repos under the same agents/ or
# work_dir/ folder are dispatchable without configuration).
WORK_DIR_ROOT = Path(
    os.environ.get("HIKARI_WORK_DIR")
    or Path(__file__).resolve().parent.parent.parent.parent
).expanduser().resolve()
# Default to read-only. If the model passes allowed_tools that includes
# Edit/Write/Bash, the gatekeeper can_use_tool gate prompts CONFIRM-SEND.
DEFAULT_ALLOWED_TOOLS = "Read,Grep,Glob,WebFetch,WebSearch"
DEFAULT_MAX_TURNS = 80
DEFAULT_BUDGET_USD = 3.00


# ----------------------------------------------------------------------
# Dispatch safety classifications
# ----------------------------------------------------------------------
# Two-tier allowlist for the LLM-supplied `allowed_tools` field:
#
#   _SAFE_DISPATCH_TOOLS         — read-only / investigative tools the LLM
#                                  may always have inside a dispatched session
#                                  without owner approval. These cannot mutate
#                                  the filesystem, run shell commands, or
#                                  exfiltrate over a side-channel.
#
#   _REQUIRES_EXPLICIT_OWNER_FLAG — code-mutation / shell / kernel-edit tools
#                                  that need the dispatch caller to set
#                                  ``write_mode=True`` AND the operator to
#                                  have already typed CONFIRM-SEND for the
#                                  enclosing dispatch_claude_session call.
#                                  Filtered out silently when write_mode=False.
#
# Anything NOT in either set is filtered out — fail-safe deny. The bridge can
# introspect these frozensets at startup to document the dispatch contract.

_SAFE_DISPATCH_TOOLS: frozenset[str] = frozenset({
    "Read", "Grep", "Glob",
    "NotebookRead",
    "WebFetch", "WebSearch",
    "TodoWrite",  # in-session task list, no filesystem effect
    "BashOutput",  # read existing background job output, doesn't spawn
    "KillShell",  # terminate the dispatched shell only — local scope
    "ListMcpResourcesTool",
    "ReadMcpResourceTool",
})

_REQUIRES_EXPLICIT_OWNER_FLAG: frozenset[str] = frozenset({
    "Bash",
    "Edit",
    "Write",
    "MultiEdit",
    "NotebookEdit",
    "Task",        # nested SDK subagent dispatch — same blast radius
    "TaskCreate",  # background routine creation
    "SlashCommand",  # can invoke arbitrary slash-defined workflows
})


# Background-task registry for fire-and-forget dispatched sessions. Without
# this, Python's GC can drop the task reference mid-flight and the dispatched
# session silently dies. Standard pattern: keep a strong ref in a set, then
# discard on done.
_BG_TASKS: set[asyncio.Task[Any]] = set()


def set_owner_chat_id(chat_id: int) -> None:
    """Called once at bridge startup."""
    global _OWNER_CHAT_ID
    _OWNER_CHAT_ID = int(chat_id)


def _owner_chat_id() -> int:
    if _OWNER_CHAT_ID is None:
        raise RuntimeError("dispatch.set_owner_chat_id() not called; bridge not started?")
    return _OWNER_CHAT_ID


def _validate_repo(repo_path: str) -> Path | None:
    """Repo must exist and be under WORK_DIR_ROOT."""
    p = Path(repo_path).expanduser().resolve()
    if not p.exists() or not p.is_dir():
        return None
    try:
        p.relative_to(WORK_DIR_ROOT)
    except ValueError:
        return None
    return p


def _filter_allowed_tools(
    requested: list[str], *, write_mode: bool,
) -> tuple[list[str], list[str]]:
    """Filter the LLM-supplied allowed_tools against the safety tiers.

    Returns ``(kept, dropped)``:
      - Always-safe tools (``_SAFE_DISPATCH_TOOLS``) pass through.
      - Write/shell tools (``_REQUIRES_EXPLICIT_OWNER_FLAG``) pass through
        ONLY when ``write_mode=True``; otherwise they're dropped.
      - Anything else is dropped (fail-safe deny: an LLM that asks for an
        unknown tool name shouldn't get it).

    Caller may log ``dropped`` so the owner can see what was filtered.
    """
    kept: list[str] = []
    dropped: list[str] = []
    for raw in requested:
        name = raw.strip()
        if not name:
            continue
        # Bare names ("Bash") and MCP-qualified names share the same policy:
        # only allow exact membership in one of the two tiers.
        if name in _SAFE_DISPATCH_TOOLS:
            kept.append(name)
        elif name in _REQUIRES_EXPLICIT_OWNER_FLAG:
            if write_mode:
                kept.append(name)
            else:
                dropped.append(name)
        else:
            # Unknown tool name — fail-safe deny. The dispatched session can
            # still use any tool registered on the SDK server side by default
            # unless allowed_tools is set; we just don't echo the unknown
            # name back into the explicit allowlist.
            dropped.append(name)
    return kept, dropped


def _build_dispatch_options(repo_path: Path, allowed_tools: list[str],
                            max_turns: int, max_budget_usd: float,
                            resume: str | None,
                            *, write_mode: bool = False) -> ClaudeAgentOptions:
    """SDK options for a dispatched session. NO subagents (flat-only), no Hikari skills.

    ``write_mode`` determines the SDK permission_mode:
      - False (default): ``permission_mode="default"`` — every Edit/Write/Bash
        the dispatched LLM tries to use triggers an interactive prompt that,
        in our headless dispatch context, will reject. This is intentional:
        without explicit owner approval upstream, dispatched code must not
        mutate anything.
      - True: ``permission_mode="acceptEdits"`` — the dispatched LLM can
        run Edit/Write/Bash autonomously. Callers MUST have already obtained
        the operator's CONFIRM-SEND on the enclosing dispatch_claude_session
        call before passing write_mode=True.
    """
    return ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        fallback_model="claude-sonnet-4-6",
        cwd=str(repo_path),
        setting_sources=["project", "user"],
        skills="all",
        system_prompt=(
            "You are a dispatched Claude Code worker. The user (via the Hikari assistant) "
            "asked you to do a specific task in this repo. Work autonomously, run tests, "
            "make edits. Be concise in your final summary."
        ),
        allowed_tools=allowed_tools,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        resume=resume,
        permission_mode="acceptEdits" if write_mode else "default",
    )


async def _emit(task_id: str, event_type: str, payload: dict[str, Any]) -> None:
    await DISPATCH_EVENTS.put((task_id, event_type, payload))


async def _run_session(task_id: str, repo_path: Path, task: str,
                       allowed_tools: list[str], max_turns: int,
                       max_budget_usd: float,
                       *, write_mode: bool = False) -> None:
    """Run a dispatched session end-to-end. All output flows through DISPATCH_EVENTS.

    ``write_mode`` is forwarded to ``_build_dispatch_options`` to pick the
    SDK ``permission_mode``. Default False = ``"default"`` (interactive
    prompt that rejects in headless dispatch). True requires the caller to
    have already obtained CONFIRM-SEND from the operator.
    """
    db.bg_task_update(task_id, status="running")
    await _emit(task_id, "started", {"repo": str(repo_path), "task": task})

    resume = db.bg_task_get(task_id).get("session_id")
    options = _build_dispatch_options(
        repo_path=repo_path, allowed_tools=allowed_tools,
        max_turns=max_turns, max_budget_usd=max_budget_usd, resume=resume,
        write_mode=write_mode,
    )

    tool_use_count = 0
    final_text_parts: list[str] = []
    total_cost: float = 0.0
    started = time.monotonic()

    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(task)
            async for msg in client.receive_response():
                if db.bg_task_cancel_requested(task_id):
                    await _emit(task_id, "cancelled", {
                        "reason": "user requested cancel",
                        "duration_s": time.monotonic() - started,
                        "tool_uses": tool_use_count,
                    })
                    db.bg_task_update(
                        task_id, status="cancelled",
                        completed_at=db._now(),
                        result_summary="cancelled by user",
                        tool_use_count=tool_use_count,
                    )
                    return
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, ToolUseBlock):
                            tool_use_count += 1
                            await _emit(task_id, "tool_use", {
                                "tool": block.name,
                                "count": tool_use_count,
                            })
                        elif isinstance(block, TextBlock):
                            final_text_parts.append(block.text)
                elif isinstance(msg, ResultMessage):
                    if msg.session_id:
                        db.bg_task_update(task_id, session_id=msg.session_id)
                    total_cost = float(msg.total_cost_usd or 0.0)
                    if msg.subtype != "success":
                        await _emit(task_id, "failed", {
                            "reason": f"subtype={msg.subtype}",
                            "cost": total_cost,
                            "duration_s": time.monotonic() - started,
                        })
                        db.bg_task_update(
                            task_id, status="failed",
                            completed_at=db._now(),
                            cost_usd=total_cost,
                            tool_use_count=tool_use_count,
                            result_summary=f"failed ({msg.subtype})",
                        )
                        return
    except Exception as e:  # noqa: BLE001
        logger.exception("dispatched session %s crashed", task_id)
        await _emit(task_id, "failed", {
            "reason": str(e), "duration_s": time.monotonic() - started,
        })
        db.bg_task_update(
            task_id, status="failed", completed_at=db._now(),
            result_summary=f"crashed: {e}", tool_use_count=tool_use_count,
        )
        return

    result_text = "".join(final_text_parts).strip()
    duration = time.monotonic() - started
    db.bg_task_update(
        task_id, status="done", completed_at=db._now(),
        result_summary=result_text[:4000], cost_usd=total_cost,
        tool_use_count=tool_use_count,
    )
    await _emit(task_id, "done", {
        "summary": result_text, "cost": total_cost,
        "duration_s": duration, "tool_uses": tool_use_count,
    })


async def _do_dispatch(args: dict[str, Any]) -> dict[str, Any]:
    """Shared dispatch body — used by both the public (gated) and confirmed
    (post-approval) tool variants. Returns the MCP envelope directly.

    Safety gates layered here:
      1. ``write_mode`` (bool, default False) — when False, any tool in
         ``_REQUIRES_EXPLICIT_OWNER_FLAG`` is silently dropped from
         ``allowed_tools`` AND the SDK permission_mode is forced to
         ``"default"`` so the dispatched LLM can't autonomously mutate.
      2. Unknown tool names are dropped (fail-safe deny).
      3. The launched asyncio task is registered in ``_BG_TASKS`` so Python's
         GC doesn't drop it mid-flight.
    """
    repo_arg = (args.get("repo_path") or "").strip()
    task_text = (args.get("task") or "").strip()
    allowed_raw_in = args.get("allowed_tools")
    if isinstance(allowed_raw_in, list):
        # Programmatic callers may pass a list already.
        raw_tokens = [str(t).strip() for t in allowed_raw_in if str(t).strip()]
    else:
        allowed_raw = (str(allowed_raw_in or "")).strip() or DEFAULT_ALLOWED_TOOLS
        raw_tokens = [t.strip() for t in allowed_raw.split(",") if t.strip()]
    max_turns = max(5, min(200, int(args.get("max_turns") or DEFAULT_MAX_TURNS)))
    write_mode = bool(args.get("write_mode") or False)

    if not task_text:
        return _ok("dispatch: task is required.")
    repo = _validate_repo(repo_arg)
    if not repo:
        return _ok(
            f"dispatch: repo_path {repo_arg!r} not found, not a dir, or outside "
            f"{WORK_DIR_ROOT}. specify an absolute path under work_dir."
        )

    allowed_tools, dropped = _filter_allowed_tools(raw_tokens, write_mode=write_mode)
    if dropped:
        logger.warning(
            "dispatch: filtered allowed_tools (write_mode=%s): kept=%s dropped=%s",
            write_mode, allowed_tools, dropped,
        )
    task_id = uuid.uuid4().hex
    chat_id = _owner_chat_id()

    db.bg_task_create(
        task_id, "claude_session", chat_id, task_text,
        meta={
            "repo": str(repo),
            "allowed_tools": allowed_tools,
            "dropped_tools": dropped,
            "write_mode": write_mode,
            "max_turns": max_turns,
        },
    )

    # Don't await — run in background. Register in _BG_TASKS so the
    # task isn't garbage-collected mid-flight (asyncio holds only a
    # weak reference once create_task returns).
    bg_task = asyncio.create_task(_run_session(
        task_id=task_id, repo_path=repo, task=task_text,
        allowed_tools=allowed_tools, max_turns=max_turns,
        max_budget_usd=DEFAULT_BUDGET_USD,
        write_mode=write_mode,
    ))
    _BG_TASKS.add(bg_task)
    bg_task.add_done_callback(_BG_TASKS.discard)

    # ETA heuristic: 30s base + 5s per estimated tool use; rough proxy from task length.
    est_uses = max(3, len(task_text) // 80)
    eta_min = max(1, (30 + 5 * est_uses) // 60)
    summary = (
        f"dispatched task {task_id[:8]} → claude session in {repo.name}. "
        f"eta ~{eta_min}m. you'll get progress + final."
    )
    if dropped:
        summary += f" (filtered {len(dropped)} unsafe tool name(s) — write_mode={write_mode})"
    return _ok(
        summary,
        data={
            "task_id": task_id,
            "eta_minutes": eta_min,
            "repo": str(repo),
            "allowed_tools": allowed_tools,
            "dropped_tools": dropped,
            "write_mode": write_mode,
        },
    )
