"""Specialist subagents Hikari delegates to via the `Agent` tool.

Each subagent is a Haiku worker (Sonnet for research) with a tight tool list
and a focused prompt. Their output is never surfaced to the user verbatim —
Hikari rewrites in voice.

Naming convention: lowercase keys in the ``agents={}`` dict passed to
``ClaudeAgentOptions``. The ``Agent`` tool sees this key as the subagent
identifier.

One file per subagent; this module re-exports the constants and the
``ALL_AGENTS`` dict so existing imports (``from .subagents import ALL_AGENTS``
in ``agents/runtime.py``, ``from agents import subagents`` in tests) keep
working unchanged.
"""

from __future__ import annotations

from claude_agent_sdk import AgentDefinition

from agents.subagents.code_dispatch import CODE_DISPATCH_AGENT
from agents.subagents.drive_gmail import DRIVE_GMAIL_AGENT
from agents.subagents.github import GITHUB_AGENT
from agents.subagents.notion import NOTION_AGENT
from agents.subagents.recall import RECALL_AGENT
from agents.subagents.research import RESEARCH_AGENT
from agents.subagents.wiki import WIKI_AGENT

ALL_AGENTS: dict[str, AgentDefinition] = {
    "recall": RECALL_AGENT,
    "wiki": WIKI_AGENT,
    "code_dispatch": CODE_DISPATCH_AGENT,
    "drive_gmail": DRIVE_GMAIL_AGENT,
    "notion": NOTION_AGENT,
    "research": RESEARCH_AGENT,
    "github": GITHUB_AGENT,
}

__all__ = [
    "ALL_AGENTS",
    "CODE_DISPATCH_AGENT",
    "DRIVE_GMAIL_AGENT",
    "GITHUB_AGENT",
    "NOTION_AGENT",
    "RECALL_AGENT",
    "RESEARCH_AGENT",
    "WIKI_AGENT",
]
