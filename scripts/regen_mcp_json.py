#!/usr/bin/env python3
"""Regenerate .mcp.json from config/tools.yaml.

Projects bucket-3 MCP servers into the .mcp.json shape. The generated file
includes a ``_generated_by`` sentinel that the validator checks to detect
hand-edits.

Usage:
    uv run python scripts/regen_mcp_json.py [--check]

    --check  Exit 1 if .mcp.json differs from what would be generated
             (for CI enforcement). No file is written.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running from repo root without installing
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools._tools_yaml import load_registry  # noqa: E402  # needs sys.path insert above

_GENERATED_SENTINEL = "tools/_tools_yaml.py via scripts/regen_mcp_json.py"

# Per-server metadata that lives in .mcp.json comments.
_SERVER_COMMENTS: dict[str, str] = {
    "google_workspace": (
        "Upstream uses OAuth user creds (NOT service account) — see "
        "scripts/setup_google_oauth.py for the one-time consent flow. "
        "Entrypoint workaround: package's own __main__ wraps a sync "
        "FastMCP.run() in asyncio.run(), so we bypass it by importing the "
        "module (which registers tools) then calling mcp.run() directly."
    ),
}

_TOP_COMMENT = (
    "External MCP servers. Hikari delegates to specialist subagents "
    "(drive_gmail, notion, research) that own these servers. The bridge logs "
    "warnings at startup if required env vars are missing."
)


class UnpinnedPackageError(RuntimeError):
    """Raised when a bucket-3 MCP server arg references a package without a pin."""


def _assert_pinned(name: str, args: list[str]) -> None:
    """Sprint 6E: refuse to write a bucket-3 entry whose package args float
    to latest. Bare ``@latest`` or no ``@version`` on npm packages and no
    ``==version`` on uvx ``--from`` args make every restart a supply-chain
    gamble. The validator looks for the package token (the one immediately
    after ``-y`` for npx, or after ``--from`` for uvx) and asserts a pin
    is present.

    Git refs (``git+https://...@<sha-or-tag>``) and local paths (``./``,
    ``/``) are accepted as-is.
    """
    if not args:
        return

    def _has_pin(token: str) -> bool:
        # Git URLs: require an explicit @<ref> after the URL path.
        if token.startswith("git+"):
            # everything after the last '@' that's not part of '://'.
            tail = token.rsplit("@", 1)[-1]
            return bool(tail) and "/" not in tail and tail != token
        # Local paths: not relevant.
        if token.startswith(("./", "../", "/")):
            return True
        # npm scoped @scope/pkg@x.y.z
        if token.startswith("@"):
            # split off the leading scope @
            rest = token[1:]
            # need at least one more '@' for the version
            if "@" not in rest:
                return False
            # forbid @latest as a pin
            return not rest.endswith("@latest")
        # uvx --from form: pkg==x.y.z
        if "==" in token:
            return True
        # npm bare package: pkg@x.y.z
        if "@" in token:
            return not token.endswith("@latest")
        return False

    # Look for the package token: after '-y' for npx, after '--from' for uvx.
    for i, a in enumerate(args):
        if a in ("-y", "--from") and i + 1 < len(args):
            pkg = args[i + 1]
            if not _has_pin(pkg):
                raise UnpinnedPackageError(
                    f"MCP server {name!r}: package arg {pkg!r} has no version "
                    f"pin. Pin via e.g. '{pkg}@1.2.3' (npm) or "
                    f"'{pkg}==1.2.3' (uvx --from) in config/tools.yaml."
                )
            return
    # No -y or --from in args — not a package launch; nothing to pin.


def build_mcp_json(registry) -> dict:
    """Build the .mcp.json dict from bucket-3 server specs."""
    servers: dict[str, dict] = {}
    for name, spec in sorted(registry.mcp_servers().items()):
        if spec.bucket != 3:
            continue
        _assert_pinned(name, list(spec.args))
        entry: dict = {}
        if name in _SERVER_COMMENTS:
            entry["_comment"] = _SERVER_COMMENTS[name]
        entry["command"] = spec.command
        entry["args"] = list(spec.args)
        if spec.env:
            entry["env"] = dict(spec.env)
        servers[name] = entry

    return {
        "_comment": _TOP_COMMENT,
        "_generated_by": _GENERATED_SENTINEL,
        "mcpServers": servers,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Regenerate .mcp.json from tools.yaml")
    parser.add_argument("--check", action="store_true",
                        help="Check mode: exit 1 if .mcp.json is stale, no write")
    args = parser.parse_args()

    registry = load_registry()
    generated = build_mcp_json(registry)
    generated_text = json.dumps(generated, indent=2, ensure_ascii=False) + "\n"

    mcp_path = REPO_ROOT / ".mcp.json"

    if args.check:
        if not mcp_path.exists():
            print("ERROR: .mcp.json does not exist; run regen_mcp_json.py to create it.")
            sys.exit(1)
        current = mcp_path.read_text(encoding="utf-8")
        # Strip _generated_by for comparison in case it's missing from a
        # pre-sentinel file. We compare the mcpServers content only.
        current_data = json.loads(current)
        generated_data = json.loads(generated_text)
        if current_data.get("mcpServers") != generated_data.get("mcpServers"):
            print("ERROR: .mcp.json is stale — run scripts/regen_mcp_json.py to regenerate.")
            print("Diff (current vs generated):")
            import difflib
            a = json.dumps(current_data.get("mcpServers"), indent=2).splitlines()
            b = json.dumps(generated_data.get("mcpServers"), indent=2).splitlines()
            for line in difflib.unified_diff(
                a, b, fromfile="current", tofile="generated", lineterm=""
            ):
                print(line)
            sys.exit(1)
        print(".mcp.json is up to date.")
        return

    mcp_path.write_text(generated_text, encoding="utf-8")
    print(f"Written: {mcp_path}")


if __name__ == "__main__":
    main()
