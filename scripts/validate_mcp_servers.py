"""Validate that .mcp.json server tools are covered by config/tools.yaml policy.

Fails closed: tools exposed by a live MCP server but not declared in the
registry (neither as an explicit entry nor under a prefix wildcard) cause
a non-zero exit. Servers whose env is missing yield a SOFT pass (skipped).

Run from CI:
    uv run python scripts/validate_mcp_servers.py --skip apple_events,apple_shortcuts
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_mcp_json() -> dict:
    mcp_json = REPO_ROOT / ".mcp.json"
    with mcp_json.open() as f:
        return json.load(f)


def _classify_policy_entries(server_name: str) -> tuple[set[str], list[str]]:
    """Return (explicit_full_tool_ids_for_server, wildcard_prefixes_for_server)."""
    from tools._tools_yaml import load_registry
    registry = load_registry()
    explicit: set[str] = set()
    wildcards: list[str] = []
    for spec in registry.specs():
        if spec.server != server_name:
            continue
        if spec.id.endswith("*"):
            wildcards.append(spec.id[:-1])
        else:
            explicit.add(spec.id)
    return explicit, wildcards


def _coverage_gaps(server_name: str, live_tools: set[str]) -> list[str]:
    explicit, wildcards = _classify_policy_entries(server_name)
    gaps: list[str] = []
    for tool_name in sorted(live_tools):
        full_id = f"mcp__{server_name}__{tool_name}"
        if full_id in explicit:
            continue
        if any(full_id.startswith(p) for p in wildcards):
            continue
        gaps.append(full_id)
    return gaps


async def _main(skip: frozenset[str], timeout: float) -> int:
    from tools.mcp_introspect import introspect_all
    mcp = _load_mcp_json()
    servers = mcp.get("mcpServers", {})
    if not servers:
        print("validate_mcp_servers: no servers in .mcp.json -- nothing to check.")
        return 0

    results = await introspect_all(servers, timeout_sec=timeout, skip=skip)
    exit_code = 0
    for server_name, result in sorted(results.items()):
        if isinstance(result, Exception):
            print(f"  {server_name}: skipped ({type(result).__name__}: {result})")
            continue
        gaps = _coverage_gaps(server_name, result)
        if gaps:
            exit_code = 1
            print(f"  {server_name}: DRIFT -- {len(gaps)} tool(s) not covered by policy:")
            for g in gaps:
                print(f"    - {g}")
        else:
            print(f"  {server_name}: OK ({len(result)} tools, all covered)")
    if exit_code != 0:
        print("\nvalidate_mcp_servers: FAIL -- add explicit gated entries or extend a wildcard.")
    else:
        print("\nvalidate_mcp_servers: clean.")
    return exit_code


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip", default="", help="comma-separated server names to skip")
    ap.add_argument("--timeout", type=float, default=10.0)
    args = ap.parse_args()
    skip = frozenset(s.strip() for s in args.skip.split(",") if s.strip())
    return asyncio.run(_main(skip, args.timeout))


if __name__ == "__main__":
    sys.exit(main())
