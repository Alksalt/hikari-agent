"""Regenerate the policy block in each subagent prompt file from config/tools.yaml.

Each subagent prompt may contain an auto-managed section delimited by:
  <!-- BEGIN AUTO-POLICY -->
  ... generated content ...
  <!-- END AUTO-POLICY -->

This script derives the canonical policy block for each subagent by:
  1. Reading config/tools.yaml to find all gated tools (gate: gatekeeper or
     gate: confirm_send) that are not wildcards and not _unsafe variants.
  2. Filtering to the subset reachable from that subagent's tool allowlist
     (defined in the subagents: block of config/tools.yaml).
  3. Formatting the filtered set as a policy snippet and writing it back
     between the markers.

--check mode exits non-zero if any prompt would change (CI-friendly).

Usage:
    uv run python scripts/regen_subagent_policy.py
    uv run python scripts/regen_subagent_policy.py --check
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BEGIN_MARKER = "<!-- BEGIN AUTO-POLICY -->"
END_MARKER = "<!-- END AUTO-POLICY -->"


def _tool_in_allowlist(tool_id: str, allowlist: tuple[str, ...]) -> bool:
    """Return True if tool_id is reachable via any entry in the subagent allowlist."""
    for pattern in allowlist:
        if pattern.endswith("*"):
            prefix = pattern[:-1]
            if tool_id.startswith(prefix):
                return True
        elif tool_id == pattern:
            return True
    return False


def _short_name(full_id: str, server: str | None) -> str:
    """Strip the mcp__<server>__ prefix to get the short tool name."""
    if server:
        prefix = f"mcp__{server}__"
        if full_id.startswith(prefix):
            return full_id[len(prefix):]
    return full_id


def _build_policy_block(subagent_id: str, tools_allowlist: tuple[str, ...]) -> str:
    """Generate the text that goes between the AUTO-POLICY markers.

    Returns an empty string (no newline) if the subagent has no gated tools
    in scope — the markers will exist but enclose nothing.
    """
    # Import here so the module is usable without the full runtime installed,
    # as long as tools/_tools_yaml.py and its deps are available.
    sys.path.insert(0, str(REPO_ROOT))
    from tools._tools_yaml import load_registry

    registry = load_registry()

    gated: list[tuple[str, str]] = []  # (short_name, gate)
    for spec in registry.specs():
        if spec.id.endswith("*"):
            continue
        if spec.id.endswith("_unsafe"):
            continue
        if spec.gate not in ("gatekeeper", "confirm_send"):
            continue
        if not _tool_in_allowlist(spec.id, tools_allowlist):
            continue
        short = _short_name(spec.id, spec.server)
        gated.append((short, spec.gate))

    if not gated:
        return ""

    gate_label = {
        "gatekeeper": "gated",
        "confirm_send": "confirm_send",
    }
    lines: list[str] = []
    lines.append("Gated tools (require owner approval before executing):")
    for short, gate in gated:
        lines.append(f"  {short} [{gate_label.get(gate, gate)}]")
    return "\n".join(lines) + "\n"


def _inject_markers(text: str, policy_body: str) -> str:
    """Return text with the AUTO-POLICY block replaced (or inserted at end).

    If markers already exist, the content between them is replaced.
    If no markers exist, they are appended at the end of the file with
    a blank line separator.
    """
    begin_idx = text.find(BEGIN_MARKER)
    end_idx = text.find(END_MARKER)

    if begin_idx != -1 and end_idx != -1 and end_idx > begin_idx:
        # Replace the content between the markers (keep markers themselves).
        before = text[: begin_idx + len(BEGIN_MARKER)]
        after = text[end_idx:]
        if policy_body:
            return before + "\n" + policy_body + after
        else:
            return before + "\n" + after
    else:
        # No markers present — append them.
        body = text.rstrip("\n") + "\n"
        if policy_body:
            body += f"\n{BEGIN_MARKER}\n{policy_body}{END_MARKER}\n"
        else:
            body += f"\n{BEGIN_MARKER}\n{END_MARKER}\n"
        return body


def _process_prompt(
    prompt_path: Path,
    subagent_id: str,
    tools_allowlist: tuple[str, ...],
    *,
    check_mode: bool,
) -> bool:
    """Process one prompt file. Returns True if file would change (or did change)."""
    original = prompt_path.read_text(encoding="utf-8")
    policy_body = _build_policy_block(subagent_id, tools_allowlist)
    updated = _inject_markers(original, policy_body)

    if updated == original:
        return False

    if check_mode:
        return True

    prompt_path.write_text(updated, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if any prompt would change (CI mode).",
    )
    args = parser.parse_args()

    sys.path.insert(0, str(REPO_ROOT))
    from tools._tools_yaml import load_registry

    registry = load_registry()
    subagent_specs = registry._subagents_spec

    repo_root_resolved = REPO_ROOT.resolve()
    drift_found: list[str] = []
    for subagent_id, spec in subagent_specs.items():
        prompt_path = REPO_ROOT / spec.prompt_path
        try:
            resolved = prompt_path.resolve()
            resolved.relative_to(repo_root_resolved)
        except ValueError:
            print(f"SECURITY: {subagent_id!r} prompt_path {spec.prompt_path!r} escapes repo root — refusing")
            return 2
        prompt_path = resolved
        if not prompt_path.exists():
            print(f"WARNING: {prompt_path} not found — skipping {subagent_id!r}")
            continue

        changed = _process_prompt(
            prompt_path,
            subagent_id,
            spec.tools,
            check_mode=args.check,
        )

        if changed:
            if args.check:
                drift_found.append(str(prompt_path.relative_to(REPO_ROOT)))
                print(f"DRIFT: {prompt_path.relative_to(REPO_ROOT)} would change")
            else:
                print(f"updated: {prompt_path.relative_to(REPO_ROOT)}")
        else:
            print(f"ok:      {prompt_path.relative_to(REPO_ROOT)}")

    if drift_found:
        print(
            f"\n{len(drift_found)} prompt(s) out of sync with config/tools.yaml. "
            "Run: uv run python scripts/regen_subagent_policy.py"
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
