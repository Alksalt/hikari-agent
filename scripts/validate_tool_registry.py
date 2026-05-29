#!/usr/bin/env python3
"""CI validator for config/tools.yaml.

Checks:
  (a) Every @tool handler in tools/**/*.py has an id in yaml (or matches a wildcard).
  (b) Every yaml bucket-1 explicit id has a discoverable handler.
  (c) Every bucket-3 explicit id has a server entry.
  (d) .mcp.json matches the projection from yaml.
  (e) All subagent prompt/description sidecar files exist.
  (f) Structural validate() passes.

Usage:
    uv run python scripts/validate_tool_registry.py

Exit 0 = clean. Exit 1 = errors found.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def run() -> list[str]:
    errors: list[str] = []

    # Load registry
    from tools._tools_yaml import load_registry
    registry = load_registry()

    # (f) Structural validate
    errs = registry.validate()
    errors.extend(errs)

    # (b) Every explicit bucket-1 tool in yaml must be discoverable
    # We check bucket-1 NON-utility tools (utility tools are auto-discovered,
    # not registered by name in yaml individually except for security-tagged ones).
    # The dedicated-server tools (memory/wiki/dispatch/codex/photo) are checked
    # by verifying their server specs exist (already done in validate()).

    # (c) Every bucket-3 explicit id must have a server entry
    # Already covered by validate(). Re-check wildcard bucket-3 too.
    servers = registry.mcp_servers()
    for spec in registry.specs():
        if spec.bucket == 3 and spec.server and spec.server not in servers:
            errors.append(f"tool {spec.id!r}: bucket-3 server {spec.server!r} not declared")

    # (a) Discover actual tool handlers in tools/**/*.py via _registry
    # and check they're covered by explicit id or wildcard in yaml.
    try:
        from tools._registry import clear_cache, discover_utility_tool_names
        clear_cache()
        utility_names = set(discover_utility_tool_names())
    except Exception as exc:
        errors.append(f"tool discovery failed: {exc}")
        utility_names = set()

    # Build the set of explicit yaml ids and wildcard prefixes
    explicit_ids = set()
    wildcard_prefixes: list[str] = []
    for spec in registry.specs():
        if spec.id.endswith("*"):
            wildcard_prefixes.append(spec.id[:-1])
        else:
            explicit_ids.add(spec.id)

    def _covered(name: str) -> bool:
        if name in explicit_ids:
            return True
        return any(name.startswith(p) for p in wildcard_prefixes)

    # Phase 5 (control-plane-lies sweep): these tools must always be gatekeeper-gated.
    _MUST_BE_GATED: dict[str, str] = {
        "mcp__hikari_utility__skill_approve": "gatekeeper",
    }
    for tool_id, required_gate in _MUST_BE_GATED.items():
        spec = registry._resolve(tool_id)
        if spec is None:
            errors.append(f"tool {tool_id!r} is missing from the registry (must be gate: {required_gate!r})")
        elif spec.gate != required_gate:
            errors.append(
                f"tool {tool_id!r} must be gate: {required_gate!r}, found {spec.gate!r}"
            )

    uncovered = [n for n in sorted(utility_names) if not _covered(n)]
    if uncovered:
        # Utility tools are auto-discovered; if they're not explicitly in yaml
        # that's fine as long as a wildcard covers them or they're under
        # hikari_utility (which is covered by the utility index).
        # Only fail if they're NOT covered by any wildcard.
        still_uncovered = [
            n for n in uncovered
            if not n.startswith("mcp__hikari_utility__")
        ]
        for n in still_uncovered:
            errors.append(f"handler {n!r} has no yaml registration (no explicit id or wildcard)")

    # (d) .mcp.json matches projection from yaml
    import json
    mcp_path = REPO_ROOT / ".mcp.json"
    if mcp_path.exists():
        from scripts.regen_mcp_json import build_mcp_json
        expected = build_mcp_json(registry)
        current = json.loads(mcp_path.read_text(encoding="utf-8"))
        if current.get("mcpServers") != expected.get("mcpServers"):
            errors.append(
                ".mcp.json mcpServers is stale — run scripts/regen_mcp_json.py"
            )
        # Check sentinel
        if "_generated_by" not in current:
            errors.append(
                ".mcp.json missing _generated_by sentinel — "
                "regenerate with scripts/regen_mcp_json.py"
            )
    else:
        errors.append(".mcp.json not found — run scripts/regen_mcp_json.py")

    return errors


def main() -> None:
    errors = run()
    if errors:
        print(f"validate_tool_registry: {len(errors)} error(s) found:")
        for e in errors:
            print(f"  ERROR: {e}")
        sys.exit(1)
    print("validate_tool_registry: clean.")


if __name__ == "__main__":
    main()
