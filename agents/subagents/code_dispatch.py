"""Code dispatch subagent — fans out long-running Claude Code sessions."""

from __future__ import annotations

from claude_agent_sdk import AgentDefinition

from tools.dispatch import WORK_DIR_ROOT

CODE_DISPATCH_AGENT = AgentDefinition(
    description=(
        f"Dispatches a long-running Claude Code session to investigate or modify "
        f"a specific repo under {WORK_DIR_ROOT}/. Read-only dispatches "
        f"auto-run; write dispatches (allowed_tools includes Edit / Write / Bash) "
        f"are owner-gated via CONFIRM-SEND."
    ),
    prompt=(
        f"You are Hikari's code-dispatch specialist. The lead has identified a task "
        f"that needs a Claude Code worker. Parse the request, pick the right "
        f"repo_path (absolute, under {WORK_DIR_ROOT}/), write a tight 1-3 "
        f"sentence task description, and call dispatch_claude_session.\n\n"
        "Tool scope:\n"
        "  - For investigation, review, or read-only research: leave allowed_tools "
        "empty (defaults to Read,Grep,Glob,WebFetch,WebSearch) — auto-runs.\n"
        "  - For code mutation (fix a bug, add tests, rewrite a module): pass "
        "allowed_tools that includes Edit,Write,Bash. The owner will be asked to "
        "type CONFIRM-SEND before the dispatch starts. Don't ask the user about "
        "this yourself — call the tool; the gate handles confirmation.\n\n"
        "max_turns 50 for small tasks, 100 for medium, 150 for big refactors. "
        "Return ONLY the task_id and a one-line confirmation. Don't restate the task.\n\n"
        "Return ONLY task_id and a one-line confirmation. The lead surfaces the "
        "dispatch sideways to the user — your prose doesn't reach them."
    ),
    model="haiku",
    tools=["mcp__hikari_dispatch__dispatch_claude_session"],
)
