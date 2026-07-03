"""``capabilities_overview`` — grouped map of what Hikari can actually do."""
from __future__ import annotations

from typing import Any

from claude_agent_sdk import tool

from tools._annotations import annotations_for
from tools._response import ok as _ok


def _hidden_domains() -> frozenset[str]:
    from agents import config as cfg
    return frozenset(cfg.get("capabilities.hidden_domains") or [])


@tool(
    "capabilities_overview",
    "Answer 'what can you do' / 'help' / 'how do i use you' — returns "
    "capability areas grouped by domain with example asks, generated from "
    "the live tool registry. Render the answer in voice: concrete, grouped, "
    "short. Never paste the raw structure.",
    {},
    annotations=annotations_for("capabilities_overview"),
)
async def capabilities_overview(args: dict[str, Any]) -> dict[str, Any]:
    from agents import config as cfg
    from tools.catalog import get_catalog

    hidden = _hidden_domains()
    groups: dict[str, dict[str, Any]] = {}
    for e in get_catalog().entries:
        if e.name.endswith("*") or e.domain in hidden:
            continue
        g = groups.setdefault(e.domain, {"tool_count": 0, "examples": []})
        g["tool_count"] += 1
        for ex in e.examples:
            # Only natural-language asks read well to the owner; skip
            # code-style usage strings like "reminder_list()".
            if "(" not in ex and len(g["examples"]) < 2 and ex not in g["examples"]:
                g["examples"].append(ex)

    areas = [
        {"domain": d, "tool_count": g["tool_count"], "examples": g["examples"]}
        for d, g in sorted(groups.items(), key=lambda kv: -kv[1]["tool_count"])
    ]
    menu = cfg.get("telegram.command_menu") or []
    data = {
        "areas": areas,
        # Skip the /help entry itself — the answer to "what can you do"
        # shouldn't suggest asking "what can you do?" again.
        "try": [
            str(m["phrase"])
            for m in menu
            if isinstance(m, dict) and m.get("phrase") and m.get("command") != "help"
        ],
    }
    return _ok(
        f"{len(areas)} capability areas, {sum(a['tool_count'] for a in areas)} tools.",
        data,
    )
