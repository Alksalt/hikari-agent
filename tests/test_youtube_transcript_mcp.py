"""YouTube Transcript MCP wiring smoke tests."""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_youtube_transcript_in_mcp_json():
    """`.mcp.json` has a `youtube_transcript` entry using uvx with a pinned
    git tag — the bare npm name `mcp-youtube-transcript` was unpublished in
    March 2025; the legitimate package is the Python jkawamoto/mcp-youtube-
    transcript distributed via PyPI/uvx. Pinning to a specific tag prevents
    a hostile force-push from swapping the code on next launch."""
    config = json.loads((REPO_ROOT / ".mcp.json").read_text())
    assert "youtube_transcript" in config["mcpServers"]
    entry = config["mcpServers"]["youtube_transcript"]
    assert entry["command"] == "uvx", (
        f"expected uvx; got {entry['command']!r}. The bare npm name is "
        "unpublished and squat-bait — use uvx with a pinned tag instead."
    )
    args_joined = " ".join(entry["args"])
    assert "jkawamoto/mcp-youtube-transcript" in args_joined, (
        f"expected the canonical jkawamoto github source in args; "
        f"got {entry['args']!r}"
    )
    assert "@v" in args_joined, (
        f"expected a pinned @vX.Y.Z tag (not branch/latest); "
        f"got {entry['args']!r}"
    )
    assert "mcp-youtube-transcript" in args_joined


def test_youtube_transcript_in_allowlist():
    """runtime.py allowlist contains mcp__youtube_transcript__*."""
    src = (REPO_ROOT / "agents" / "runtime.py").read_text()
    assert "mcp__youtube_transcript__*" in src


def test_youtube_transcript_in_wrap_patterns():
    """engagement.yaml prompt_injection.wrap_patterns contains a regex
    matching mcp__youtube_transcript__* — transcript content is external."""
    cfg = yaml.safe_load((REPO_ROOT / "config" / "engagement.yaml").read_text())
    patterns = cfg["prompt_injection"]["wrap_patterns"]
    matched = any(
        re.match(pat, "mcp__youtube_transcript__get_transcript")
        for pat in patterns
    )
    assert matched, f"no wrap_pattern matched mcp__youtube_transcript__*; patterns: {patterns}"
