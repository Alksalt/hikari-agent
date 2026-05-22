"""Single-source tool registry loaded from config/tools.yaml.

Provides:
  - ToolSpec         — frozen dataclass describing one tool entry
  - McpServerSpec    — frozen dataclass describing one MCP server
  - SubagentSpec     — frozen dataclass describing one subagent
  - ToolRegistry     — resolved registry with query methods
  - load_registry()  — @cache factory; optional path override for tests

Design notes
------------
Wildcard ids (ending with ``*``) are lower priority than explicit ids.
Lookup is longest-prefix match among wildcards when no explicit id wins.

The registry is read-only / immutable after construction. Callers that
need the downstream objects (AgentDefinition, etc.) call the specific
``subagents()`` / ``mcp_servers()`` helpers which produce those objects
from the raw spec data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_YAML_PATH = REPO_ROOT / "config" / "tools.yaml"


# ---------------------------------------------------------------------------
# Specs (frozen dataclasses — immutable value objects)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class McpServerSpec:
    name: str
    bucket: int                        # 1 = in-process, 3 = external
    runtime_factory: str | None        # "module:callable" for bucket-1
    conditional: bool                  # attach only when tools intersect
    command: str | None                # for bucket-3
    args: tuple[str, ...]              # for bucket-3
    env: dict[str, str]               # from_env refs kept as raw strings
    allowlist: tuple[str, ...]         # not used yet; reserved for validator


@dataclass(frozen=True)
class ToolSpec:
    id: str
    bucket: int
    server: str | None
    gate: str | None                   # null | defer | gatekeeper
    untrusted_output: bool
    wrap_patterns: tuple[str, ...]


@dataclass(frozen=True)
class SubagentSpec:
    id: str
    model: str
    tools: tuple[str, ...]
    description_path: str
    prompt_path: str


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class ToolRegistry:
    """Resolved view of config/tools.yaml.

    All heavy parsing happens once at construction time; query methods
    are O(1) or O(n-tools) but always return pre-computed results.
    """

    def __init__(
        self,
        tool_specs: list[ToolSpec],
        server_specs: list[McpServerSpec],
        subagent_specs: list[SubagentSpec],
        repo_root: Path,
    ) -> None:
        self._tools = tool_specs
        self._servers = {s.name: s for s in server_specs}
        self._subagents_spec = {s.id: s for s in subagent_specs}
        self._repo_root = repo_root

        # Separate explicit ids (no wildcard) from wildcard patterns.
        self._explicit: dict[str, ToolSpec] = {}
        self._wildcards: list[ToolSpec] = []  # ordered longest-prefix first
        for spec in tool_specs:
            if spec.id.endswith("*"):
                self._wildcards.append(spec)
            else:
                self._explicit[spec.id] = spec
        # Sort wildcards by prefix length descending so first match wins.
        self._wildcards.sort(key=lambda s: len(s.id), reverse=True)

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def _resolve(self, tool_name: str) -> ToolSpec | None:
        """Return the best-matching ToolSpec for a fully-qualified tool name."""
        if tool_name in self._explicit:
            return self._explicit[tool_name]
        for wc in self._wildcards:
            wc_prefix = wc.id[:-1]  # strip trailing "*"
            if tool_name.startswith(wc_prefix):
                return wc
        return None

    # ------------------------------------------------------------------
    # Public query methods
    # ------------------------------------------------------------------

    def allowed_tool_names(self) -> list[str]:
        """Ordered list of tool names / wildcards for the SDK allowlist.

        This is the registry-driven replacement for
        ``_DEDICATED_AND_EXTERNAL_TOOLS`` in ``agents/runtime.py``.
        Utility tool names (auto-discovered from ``tools/``) are NOT
        included here — the caller appends them via
        ``tools._registry.discover_utility_tool_names()``.
        """
        seen: set[str] = set()
        out: list[str] = []
        for spec in self._tools:
            name = spec.id
            # Utility wildcard auto-discovery handles hikari_utility tools;
            # explicit ones registered here are still included.
            if name not in seen:
                seen.add(name)
                out.append(name)
        return out

    def defer_gated_patterns(self) -> list[str]:
        """Regex patterns for tools gated with ``gate: defer``.

        Replaces ``approvals.defer_gated_tools`` in engagement.yaml.
        Returns fullmatch-style patterns — exact same shape that
        ``_is_defer_gated`` expects.
        """
        out: list[str] = []
        for spec in self._tools:
            if spec.gate == "defer":
                if spec.id.endswith("*"):
                    # Convert wildcard to regex prefix
                    prefix = re.escape(spec.id[:-1])
                    out.append(f"^{prefix}.*$")
                else:
                    out.append(f"^{re.escape(spec.id)}$")
        return out

    def wrap_patterns(self) -> list[str]:
        """Merged list of regex wrap patterns from all tool specs.

        Replaces ``prompt_injection.wrap_patterns`` in engagement.yaml.
        """
        seen: set[str] = set()
        out: list[str] = []
        for spec in self._tools:
            for pat in spec.wrap_patterns:
                if pat not in seen:
                    seen.add(pat)
                    out.append(pat)
        return out

    def untrusted_tools(self) -> list[str]:
        """Substring prefixes / patterns for ``is_untrusted_source``.

        Replaces ``prompt_injection.untrusted_tools`` in engagement.yaml.
        Returns the tool id (without trailing ``*``) — matches the
        substring-match semantics of the existing ``is_untrusted_source``.
        """
        seen: set[str] = set()
        out: list[str] = []
        for spec in self._tools:
            if not spec.untrusted_output:
                continue
            name = spec.id
            if name.endswith("*"):
                name = name[:-1]
            if name not in seen:
                seen.add(name)
                out.append(name)
        return out

    def subagents(self) -> dict[str, Any]:
        """Return ``{name: AgentDefinition}`` for all registered subagents.

        Lazy-imports ``claude_agent_sdk.AgentDefinition`` to avoid a
        hard dep at import time (tests may not have the SDK installed).
        """
        from claude_agent_sdk import AgentDefinition

        result: dict[str, AgentDefinition] = {}
        for sid, spec in self._subagents_spec.items():
            desc_path = self._repo_root / spec.description_path
            prompt_path = self._repo_root / spec.prompt_path
            description = desc_path.read_text(encoding="utf-8").strip()
            prompt = prompt_path.read_text(encoding="utf-8").strip()
            result[sid] = AgentDefinition(
                description=description,
                prompt=prompt,
                model=spec.model,
                tools=list(spec.tools),
            )
        return result

    def mcp_servers(self) -> dict[str, McpServerSpec]:
        """Return all server specs keyed by name."""
        return dict(self._servers)

    def specs(self) -> list[ToolSpec]:
        """Return all tool specs (explicit + wildcard, in definition order)."""
        return list(self._tools)

    def server_spec(self, name: str) -> McpServerSpec | None:
        return self._servers.get(name)

    def validate(self) -> list[str]:
        """Run structural validation. Returns a list of error strings (empty = clean)."""
        errors: list[str] = []
        # Every explicit bucket-3 tool must have a server entry
        for spec in self._tools:
            if spec.bucket == 3 and spec.server and spec.server not in self._servers:
                errors.append(
                    f"tool {spec.id!r}: references unknown server {spec.server!r}"
                )
        # Every subagent prompt/description file must exist
        for sid, spec in self._subagents_spec.items():
            for attr, path_str in [
                ("description_path", spec.description_path),
                ("prompt_path", spec.prompt_path),
            ]:
                p = self._repo_root / path_str
                if not p.exists():
                    errors.append(f"subagent {sid!r}: {attr} {path_str!r} not found")
        return errors


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_env(raw: dict | None) -> dict[str, str]:
    """Convert ``{KEY: {from_env: KEY}}`` map to ``{KEY: "${KEY}"}`` for storage."""
    if not raw:
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        if isinstance(v, dict) and "from_env" in v:
            # Keep as ${VAR} reference — don't resolve at load time
            out[str(k)] = f"${{{v['from_env']}}}"
        elif isinstance(v, str):
            out[str(k)] = v
        else:
            out[str(k)] = str(v)
    return out


def _parse_server(name: str, raw: dict) -> McpServerSpec:
    return McpServerSpec(
        name=name,
        bucket=int(raw.get("bucket", 1)),
        runtime_factory=raw.get("runtime_factory"),
        conditional=bool(raw.get("conditional", False)),
        command=raw.get("command"),
        args=tuple(raw.get("args") or []),
        env=_parse_env(raw.get("env")),
        allowlist=tuple(raw.get("allowlist") or []),
    )


def _parse_tool(raw: dict) -> ToolSpec:
    return ToolSpec(
        id=str(raw["id"]),
        bucket=int(raw.get("bucket", 1)),
        server=raw.get("server"),
        gate=raw.get("gate"),
        untrusted_output=bool(raw.get("untrusted_output", False)),
        wrap_patterns=tuple(raw.get("wrap_patterns") or []),
    )


def _parse_subagent(sid: str, raw: dict) -> SubagentSpec:
    return SubagentSpec(
        id=sid,
        model=str(raw.get("model", "haiku")),
        tools=tuple(raw.get("tools") or []),
        description_path=str(raw.get("description_path", "")),
        prompt_path=str(raw.get("prompt_path", "")),
    )


def _load_yaml(path: Path) -> ToolRegistry:
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)

    server_specs = [
        _parse_server(name, srv)
        for name, srv in (data.get("mcp_servers") or {}).items()
    ]

    tool_specs = [
        _parse_tool(t)
        for t in (data.get("tools") or [])
    ]

    subagent_specs = [
        _parse_subagent(sid, sa)
        for sid, sa in (data.get("subagents") or {}).items()
    ]

    return ToolRegistry(
        tool_specs=tool_specs,
        server_specs=server_specs,
        subagent_specs=subagent_specs,
        repo_root=path.parent.parent,
    )


@cache
def load_registry(path: Path | None = None) -> ToolRegistry:
    """Load and return the cached ToolRegistry.

    ``path`` defaults to ``config/tools.yaml`` relative to the repo root.
    Pass an explicit Path in tests to use a fixture yaml file. The cache
    is keyed on the path so test overrides work without busting the
    production cache.
    """
    resolved = path if path is not None else DEFAULT_YAML_PATH
    return _load_yaml(resolved)
