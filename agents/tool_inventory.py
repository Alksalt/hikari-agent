"""Live tool inventory injected into Hikari's per-turn context.

Without this block, Hikari free-associates about her tool surface
("blocked on your allowlist", "apple events keeps dying") because nothing
tells her what she actually has. The runtime uses
``permission_mode="acceptEdits"`` — there is no Claude Code allowlist
applying here; external connectors fail because of unset env vars, not
"approval".

The enumerator is purely synchronous and re-read on every call (cheap —
just env lookups + a static list).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_JSON_PATH = REPO_ROOT / ".mcp.json"


def _in_process_groups() -> dict[str, list[str]]:
    """Bucket each allowlisted in-process tool name by its server prefix.

    Tool names look like ``mcp__hikari_memory__recall`` — we split on
    ``__`` and group by the server segment (``memory``). Native tools
    (``Agent``, ``Read``, ``Glob``, ``Grep``) and wildcard external
    grants are skipped here — they're handled in
    ``_external_mcp_status``.
    """
    # Lazy import to avoid a circular dep: hooks -> tool_inventory ->
    # runtime -> hooks. runtime imports hooks at module level, so we can
    # only reach into it once both modules are fully initialised.
    from . import runtime as runtime_mod

    groups: dict[str, list[str]] = {}
    for raw in runtime_mod.allowed_tool_names():
        if not raw.startswith("mcp__hikari_"):
            continue
        if raw.endswith("*"):
            continue
        parts = raw.split("__")
        if len(parts) < 3:
            continue
        server = parts[1].replace("hikari_", "")
        tool = "__".join(parts[2:])
        groups.setdefault(server, []).append(tool)
    for server in groups:
        groups[server].sort()
    return groups


def _load_mcp_servers() -> dict[str, dict]:
    """Read .mcp.json once per call. Tiny file."""
    try:
        raw = MCP_JSON_PATH.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    servers = data.get("mcpServers") or {}
    return {k: v for k, v in servers.items() if not k.startswith("_")}


def _external_mcp_status() -> list[tuple[str, str]]:
    """For each external MCP server declared in .mcp.json, return
    ``(name, status_string)`` describing whether its env vars are present.
    """
    out: list[tuple[str, str]] = []
    servers = _load_mcp_servers()
    for name in sorted(servers):
        env_block = servers[name].get("env") or {}
        if not env_block:
            out.append((name, "n/a (no auth)"))
            continue
        # env values look like "${VAR_NAME}" — extract the var names.
        missing: list[str] = []
        for _key, value in env_block.items():
            if not isinstance(value, str):
                continue
            v = value.strip()
            if v.startswith("${") and v.endswith("}"):
                var_name = v[2:-1]
                if not os.environ.get(var_name):
                    missing.append(var_name)
            elif not v:
                # Inlined empty value — treat as unset.
                missing.append(_key)
        if missing:
            out.append((name, f"unconfigured (set {', '.join(missing)})"))
        else:
            out.append((name, "configured"))
    return out


def _subagents() -> list[tuple[str, str]]:
    from . import subagents as subagents_mod

    out: list[tuple[str, str]] = []
    for name, agent in subagents_mod.ALL_AGENTS.items():
        desc = (agent.description or "").strip().splitlines()[0] if agent.description else ""
        if len(desc) > 100:
            desc = desc[:97] + "..."
        out.append((name, desc))
    return out


def format_for_injection() -> str:
    """Render a ``# tools available`` block for ``inject_memory``.

    Block style matches the existing ``# memory: <name>`` headers in
    ``agents/hooks.py`` — one h1 header, then short lines. Stays under
    ~30 lines so it doesn't crowd higher-priority context.
    """
    lines: list[str] = ["# tools available"]

    lines.append("in-process (always working, no auth):")
    groups = _in_process_groups()
    for server in sorted(groups):
        tools = groups[server]
        lines.append(f"- {server}: {', '.join(tools)}")

    lines.append("external mcp (status reflects env-var presence at boot):")
    for name, status in _external_mcp_status():
        lines.append(f"- {name}: {status}")

    lines.append("subagents (delegate via the Agent tool):")
    for name, desc in _subagents():
        if desc:
            lines.append(f"- {name} — {desc}")
        else:
            lines.append(f"- {name}")

    lines.append(
        "note: there is no claude-code allowlist applying here "
        "(permission_mode=acceptEdits). when an external mcp call fails, "
        "it's an env-var or auth issue, not 'approval'."
    )
    return "\n".join(lines)
