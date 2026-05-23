"""Spawn each bucket-3 MCP server, send tools/list, return tool names.

Used by CI/preflight (scripts/validate_mcp_servers.py). NOT invoked at
agent runtime -- too costly + depends on external server liveness.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_INIT_MSG = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "hikari-introspect", "version": "1.0"},
    },
}

_LIST_MSG = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


async def list_server_tools(
    command: str,
    args: tuple[str, ...],
    *,
    env: dict[str, str] | None = None,
    timeout_sec: float = 10.0,
) -> set[str]:
    """Spawn one MCP server via stdio, send tools/list, return the tool names.

    Raises asyncio.TimeoutError on timeout. Raises on subprocess spawn failure
    or JSON-RPC error. Always terminates the subprocess in finally.
    """
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    proc = await asyncio.create_subprocess_exec(
        command, *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=full_env,
    )
    try:
        async def _io() -> set[str]:
            assert proc.stdin and proc.stdout
            proc.stdin.write((json.dumps(_INIT_MSG) + "\n").encode())
            await proc.stdin.drain()
            init_line = await proc.stdout.readline()
            if not init_line:
                raise RuntimeError("MCP server did not respond to initialize")
            proc.stdin.write((json.dumps(_LIST_MSG) + "\n").encode())
            await proc.stdin.drain()
            list_line = await proc.stdout.readline()
            if not list_line:
                raise RuntimeError("MCP server did not respond to tools/list")
            payload = json.loads(list_line.decode())
            tools = payload.get("result", {}).get("tools", []) or []
            return {t["name"] for t in tools if isinstance(t, dict) and "name" in t}
        return await asyncio.wait_for(_io(), timeout=timeout_sec)
    finally:
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass


async def introspect_all(
    servers: dict[str, dict[str, Any]],
    *,
    timeout_sec: float = 10.0,
    skip: frozenset[str] = frozenset(),
) -> dict[str, set[str] | Exception]:
    """Best-effort introspect every server in parallel.

    `servers` keys are server names; values are dicts with command, args, env.
    Returns mapping of server name to either tool-name set or the exception.
    Servers in `skip` are omitted entirely (returns no key for them).
    """
    results: dict[str, set[str] | Exception] = {}

    async def _one(name: str, spec: dict[str, Any]) -> None:
        if name in skip:
            return
        try:
            results[name] = await list_server_tools(
                spec["command"],
                tuple(spec.get("args", [])),
                env=spec.get("env"),
                timeout_sec=timeout_sec,
            )
        except Exception as exc:
            results[name] = exc

    await asyncio.gather(*[_one(n, s) for n, s in servers.items()])
    return results
