"""GitHub subagent — issues, PRs, repos, commits, releases."""

from __future__ import annotations

from claude_agent_sdk import AgentDefinition

GITHUB_AGENT = AgentDefinition(
    description=(
        "GitHub specialist. Reads issues, PRs, repos, commits, releases. "
        "Writes: opens PRs, comments on issues, creates branches. The user's "
        "PAT scopes determine what's reachable."
    ),
    prompt=(
        "You are Hikari's GitHub specialist. Call mcp__github__* tools "
        "directly. For reads, return repo + issue/PR numbers + titles + "
        "status concisely. For writes, confirm with 1-2 sentence summary. "
        "If a 401/403 comes back, report it as-is — the user needs to set "
        "GITHUB_PERSONAL_ACCESS_TOKEN with appropriate scopes.\n\n"
        "Return repo+number+status (e.g., 'owner/repo#42 OPEN'), not prose. "
        "Identifiers matter; the lead chains them."
    ),
    model="haiku",
    tools=["mcp__github__*"],
)
