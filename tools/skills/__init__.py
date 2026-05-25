"""Skills management + execution — hikari_utility auto-discovered tools.

Exposes skill_list, skill_read, skill_create, skill_approve, and run_skill
on the hikari_utility MCP server. Skills live in .agents/skills/<id>/SKILL.md.
"""
from __future__ import annotations

from tools.skills.core import (
    run_skill,
    skill_approve,
    skill_create,
    skill_list,
    skill_read,
)

ALL_TOOLS = [skill_list, skill_read, skill_create, skill_approve, run_skill]
