"""dispatch_claude_session — spawn a long-running Claude Code session as a nested
ClaudeSDKClient inside Hikari's process.

Architecture choice: a NESTED SDK client (not a CLI subprocess). Same OAuth token,
native message types (AssistantMessage / ToolUseBlock / ResultMessage), trivial cost
extraction. Trade-off: dies if Hikari restarts — recovered via session_id resume.

Each call:
  1. Validates repo_path (must exist + under WORK_DIR_ROOT — see env HIKARI_WORK_DIR).
  2. Pre-allocates task_id = uuid.
  3. Inserts background_tasks row (status='queued').
  4. Spawns an asyncio.Task that runs the session; pushes events to a global queue
     consumed by agents/background_listener.
  5. Returns immediately with {task_id, eta} so Hikari can ack the user.

The listener (started in telegram_bridge post_init) drains the queue and sends
in-voice progress / completion updates to Telegram.
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
    tool,
)

from storage import db

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
    or Path(__file__).resolve().parent.parent.parent
).expanduser().resolve()
# Phase 8: default to read-only. If the model passes allowed_tools that
# includes Edit/Write/Bash, the PreToolUse arg-gate (config:
# approvals.defer_when_args_match) defers the call until the owner types
# CONFIRM-SEND. After approval the resume invokes the sibling
# `dispatch_claude_session_confirmed` which carries the requested allowlist
# verbatim and skips the gate.
DEFAULT_ALLOWED_TOOLS = "Read,Grep,Glob,WebFetch,WebSearch"
DEFAULT_MAX_TURNS = 80
DEFAULT_BUDGET_USD = 3.00


def set_owner_chat_id(chat_id: int) -> None:
    """Called once at bridge startup."""
    global _OWNER_CHAT_ID
    _OWNER_CHAT_ID = int(chat_id)


def _owner_chat_id() -> int:
    if _OWNER_CHAT_ID is None:
        raise RuntimeError("dispatch.set_owner_chat_id() not called; bridge not started?")
    return _OWNER_CHAT_ID


def _ok(text: str, data: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if data is not None:
        body["data"] = data
    return body


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


def _build_dispatch_options(repo_path: Path, allowed_tools: list[str],
                            max_turns: int, max_budget_usd: float,
                            resume: str | None) -> ClaudeAgentOptions:
    """SDK options for a dispatched session. NO subagents (flat-only), no Hikari skills."""
    return ClaudeAgentOptions(
        model="claude-sonnet-4-6",
        fallback_model="claude-haiku-4-5",
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
        permission_mode="acceptEdits",
    )


async def _emit(task_id: str, event_type: str, payload: dict[str, Any]) -> None:
    await DISPATCH_EVENTS.put((task_id, event_type, payload))


async def _run_session(task_id: str, repo_path: Path, task: str,
                       allowed_tools: list[str], max_turns: int,
                       max_budget_usd: float) -> None:
    """Run a dispatched session end-to-end. All output flows through DISPATCH_EVENTS."""
    db.bg_task_update(task_id, status="running")
    await _emit(task_id, "started", {"repo": str(repo_path), "task": task})

    resume = db.bg_task_get(task_id).get("session_id")
    options = _build_dispatch_options(
        repo_path=repo_path, allowed_tools=allowed_tools,
        max_turns=max_turns, max_budget_usd=max_budget_usd, resume=resume,
    )

    tool_use_count = 0
    final_text_parts: list[str] = []
    total_cost: float = 0.0
    started = time.monotonic()

    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(task)
            async for msg in client.receive_response():
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
    (post-approval) tool variants. Returns the MCP envelope directly."""
    repo_arg = (args.get("repo_path") or "").strip()
    task_text = (args.get("task") or "").strip()
    allowed_raw = (args.get("allowed_tools") or "").strip() or DEFAULT_ALLOWED_TOOLS
    max_turns = max(5, min(200, int(args.get("max_turns") or DEFAULT_MAX_TURNS)))

    if not task_text:
        return _ok("dispatch: task is required.")
    repo = _validate_repo(repo_arg)
    if not repo:
        return _ok(
            f"dispatch: repo_path {repo_arg!r} not found, not a dir, or outside "
            f"{WORK_DIR_ROOT}. specify an absolute path under work_dir."
        )

    allowed_tools = [t.strip() for t in allowed_raw.split(",") if t.strip()]
    task_id = uuid.uuid4().hex
    chat_id = _owner_chat_id()

    db.bg_task_create(
        task_id, "claude_session", chat_id, task_text,
        meta={"repo": str(repo), "allowed_tools": allowed_tools, "max_turns": max_turns},
    )

    # Don't await — run in background.
    asyncio.create_task(_run_session(
        task_id=task_id, repo_path=repo, task=task_text,
        allowed_tools=allowed_tools, max_turns=max_turns,
        max_budget_usd=DEFAULT_BUDGET_USD,
    ))

    # ETA heuristic: 30s base + 5s per estimated tool use; rough proxy from task length.
    est_uses = max(3, len(task_text) // 80)
    eta_min = max(1, (30 + 5 * est_uses) // 60)
    return _ok(
        f"dispatched task {task_id[:8]} → claude session in {repo.name}. "
        f"eta ~{eta_min}m. you'll get progress + final.",
        data={"task_id": task_id, "eta_minutes": eta_min, "repo": str(repo)},
    )


@tool(
    "dispatch_claude_session",
    "Spawn a background CODE-MODIFICATION / repo-investigation session in a SEPARATE "
    "repo on the user's Mac Mini. NOT a way to answer a chat question — this fires "
    "an autonomous Claude Code worker on a codebase and the user gets progress + "
    "final via Telegram async (minutes, not seconds). Default allowed_tools is "
    "read-only; adding Edit/Write/Bash triggers a CONFIRM-SEND owner gate. "
    f"repo_path must be absolute and under {WORK_DIR_ROOT}/. "
    "e.g. user says 'go look at the meria repo and patch the auth bug' → dispatch. "
    "Don't use this to answer a question with public-web info (use `research`) or "
    "to look up something in the user's notes (use `wiki_search`).",
    {"repo_path": str, "task": str, "allowed_tools": str, "max_turns": int},
)
async def dispatch_claude_session(args: dict[str, Any]) -> dict[str, Any]:
    return await _do_dispatch(args)


@tool(
    "dispatch_claude_session_confirmed",
    "POST-APPROVAL execution path for dispatch_claude_session — performs the "
    "actual dispatch WITHOUT going through the approval gate. NOT in Hikari's "
    "default allowed_tools; only injected per-turn by the defer-resume codepath "
    "in tools/approvals._resume_after_defer. If you (the lead agent) see this "
    "tool in your allowlist, you were invoked via the resume path; call it once "
    "with the args from the system prompt and stop.",
    {"repo_path": str, "task": str, "allowed_tools": str, "max_turns": int},
)
async def dispatch_claude_session_confirmed(args: dict[str, Any]) -> dict[str, Any]:
    return await _do_dispatch(args)


# Public tools — registered on the always-on `hikari_dispatch` MCP server.
PUBLIC_TOOLS = [dispatch_claude_session]

# Phase 8: confirmed-sibling for the dispatch arg-gate. Lives on a separate
# `hikari_dispatch_confirmed` MCP server attached only during the resume turn
# (see agents/runtime._dispatch_confirmed_server).
CONFIRMED_TOOLS = [dispatch_claude_session_confirmed]

# Backwards-compat alias.
ALL_TOOLS = PUBLIC_TOOLS
