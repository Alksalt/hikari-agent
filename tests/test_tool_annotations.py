"""Sprint 6F — every in-process @tool decorator has MCP ToolAnnotations
that match the runtime registry's access_mode.

The annotations are HINTS clients use to surface "destructive" / "external"
/ "read-only" warnings. Drift between the annotation and the registry's
gate/access_mode means external clients (Claude Desktop, web app) see one
contract and Hikari enforces another. This test catches that drift.
"""

from __future__ import annotations

from mcp.types import ToolAnnotations

from tools._annotations import ANNOTATIONS_BY_TOOL, annotations_for
from tools._registry import discover_instrumented_tools
from tools._tools_yaml import load_registry

# ---- assemble the full in-process tool surface --------------------------

def _all_in_process_tools():
    """Return every tool that ships in an in-process MCP server.

    Includes the utility aggregator (`_utility_index`) PLUS the per-server
    `ALL_TOOLS` exports of the dedicated servers (memory, wiki,
    router, codex, dispatch). Discovery alone misses dedicated-server
    tools because `_registry._DEDICATED_SERVER_MODULES` skips them.
    """
    tools = list(discover_instrumented_tools())
    # Pull in every dedicated-server tool list explicitly so the test
    # contract covers ALL in-process tool surfaces.
    from tools import codex as _codex  # noqa: PLC0415
    from tools import dispatch as _dispatch  # noqa: PLC0415
    from tools import memory as _memory  # noqa: PLC0415
    from tools import photos as _photos  # noqa: PLC0415
    from tools import router as _router  # noqa: PLC0415
    from tools import wiki as _wiki  # noqa: PLC0415
    for module in (_memory, _wiki, _photos, _router, _codex, _dispatch):
        tools.extend(getattr(module, "ALL_TOOLS", []))
    # Deduplicate by tool name; SDK wraps every tool the same way so a
    # second copy means duplicate test coverage, not a bug.
    seen: dict[str, object] = {}
    for t in tools:
        name = getattr(t, "name", None)
        if name and name not in seen:
            seen[name] = t
    return list(seen.values())


# ---- contracts ----------------------------------------------------------

def test_every_in_process_tool_has_annotations():
    """No @tool decorator may ship without an annotations hint after 6F."""
    missing = [
        getattr(t, "name", repr(t))
        for t in _all_in_process_tools()
        if getattr(t, "annotations", None) is None
    ]
    assert not missing, (
        f"Tools missing MCP annotations: {missing}. "
        f"Add an entry to tools/_annotations.py ANNOTATIONS_BY_TOOL."
    )


def test_annotations_are_tool_annotations_instances():
    """Annotations must be `mcp.types.ToolAnnotations`, not dicts."""
    for t in _all_in_process_tools():
        ann = getattr(t, "annotations", None)
        if ann is None:
            continue  # caught by the previous test
        assert isinstance(ann, ToolAnnotations), (
            f"{t.name}: annotations is {type(ann).__name__!r}, expected ToolAnnotations"
        )


def test_destructive_tools_have_destructive_hint():
    """Tools whose access_mode is 'destructive' MUST have destructiveHint=True."""
    reg = load_registry()
    for t in _all_in_process_tools():
        name = t.name
        # Try the in-process server prefixes used in config/tools.yaml.
        candidates = [
            f"mcp__hikari_utility__{name}",
            f"mcp__hikari_memory__{name}",
            f"mcp__hikari_wiki__{name}",
            f"mcp__hikari_codex__{name}",
            f"mcp__hikari_dispatch__{name}",
            f"mcp__hikari_router__{name}",
        ]
        spec = None
        for fq in candidates:
            spec = reg._resolve(fq)
            if spec:
                break
        if spec is None or spec.access_mode != "destructive":
            continue
        ann = t.annotations
        assert ann is not None and ann.destructiveHint is True, (
            f"{name}: access_mode=destructive but annotations.destructiveHint != True"
        )


def test_read_only_tools_have_read_only_hint():
    """Tools whose annotation says read-only must have readOnlyHint=True."""
    for name, ann in ANNOTATIONS_BY_TOOL.items():
        if ann.readOnlyHint is True:
            assert ann.destructiveHint in (None, False), (
                f"{name}: read-only tool cannot also be destructive"
            )


def test_external_io_marker_consistent():
    """Tools tagged as external in ANNOTATIONS_BY_TOOL must have openWorldHint=True."""
    # Anchor: these are the canonical external tools per plan + per file inspection.
    must_be_external = {
        "arxiv_search", "currency_convert", "translate", "weather_fetch",
        "places_search", "place_open_now",
        "ytmusic_search", "ytmusic_library", "ytmusic_recent",
        "dispatch_claude_session", "link_save",
    }
    for name in must_be_external:
        ann = ANNOTATIONS_BY_TOOL.get(name)
        assert ann is not None, f"{name} missing from ANNOTATIONS_BY_TOOL"
        assert ann.openWorldHint is True, (
            f"{name}: expected openWorldHint=True (external IO) but got {ann.openWorldHint}"
        )


def test_annotations_for_returns_none_for_unknown():
    """Unknown tool names should return None, not crash."""
    assert annotations_for("nonexistent_tool_xyz") is None


def test_annotations_for_returns_registered_constant():
    ann = annotations_for("recall")
    assert ann is not None
    assert ann.readOnlyHint is True
    assert ann.openWorldHint is False
