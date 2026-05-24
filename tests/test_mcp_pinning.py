"""Sprint 6E — assert every bucket-3 MCP server arg is pinned.

A bare `@latest` or no `@version` makes restart a supply-chain gamble:
each `npx -y` / `uvx --from` re-resolves the package against the public
index. A hostile or malicious version published between launches would
land on disk and run with the bot's privileges before the operator could
notice.

This test reads the canonical source (config/tools.yaml via
tools._tools_yaml.load_registry()) and asserts every package arg has a
version pin. Also re-runs the same validator on the generated .mcp.json
to defend against future hand-edits.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.regen_mcp_json import UnpinnedPackageError, _assert_pinned
from tools._tools_yaml import load_registry

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_every_bucket_3_mcp_server_has_pinned_args():
    registry = load_registry()
    for name, spec in registry.mcp_servers().items():
        if spec.bucket != 3:
            continue
        # Must NOT raise.
        _assert_pinned(name, list(spec.args))


def test_mcp_json_packages_all_pinned():
    """Defend against hand-edits to .mcp.json by re-validating its args."""
    mcp_json = json.loads((REPO_ROOT / ".mcp.json").read_text("utf-8"))
    servers = mcp_json.get("mcpServers", {})
    assert servers, "no mcpServers in .mcp.json — generator broken"
    for name, entry in servers.items():
        _assert_pinned(name, list(entry.get("args", [])))


def test_validator_rejects_bare_npm_package():
    with pytest.raises(UnpinnedPackageError, match="no version pin"):
        _assert_pinned("test_srv", ["-y", "@notionhq/notion-mcp-server"])


def test_validator_rejects_at_latest():
    with pytest.raises(UnpinnedPackageError, match="no version pin"):
        _assert_pinned("test_srv", ["-y", "@playwright/mcp@latest"])


def test_validator_rejects_uvx_unpinned():
    with pytest.raises(UnpinnedPackageError, match="no version pin"):
        _assert_pinned("test_srv", ["--from", "google-workspace-mcp", "python", "-c", "..."])


def test_validator_accepts_scoped_npm_pin():
    # Must NOT raise
    _assert_pinned("test_srv", ["-y", "@notionhq/notion-mcp-server@1.4.0"])


def test_validator_accepts_bare_npm_pin():
    _assert_pinned("test_srv", ["-y", "mcp-server-apple-events@2.2.1"])


def test_validator_accepts_uvx_equals_pin():
    _assert_pinned("test_srv", ["--from", "google-workspace-mcp==2.0.1"])


def test_validator_accepts_git_url_with_ref():
    _assert_pinned(
        "test_srv",
        ["--from", "git+https://github.com/jkawamoto/mcp-youtube-transcript@v0.6.4"],
    )


def test_validator_skips_servers_with_no_package_args():
    """Servers without `-y` or `--from` (e.g. raw binaries) don't need pinning."""
    _assert_pinned("test_srv", ["my-bot", "--port", "8080"])
