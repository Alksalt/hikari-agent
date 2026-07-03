"""Sprint 6F — MCP ToolAnnotations for in-process @tool decorators.

MCP defines a small set of hint flags clients use to surface "this is a
destructive call" / "this hits the network" / "this only reads" without
having to inspect the tool description. We mirror them onto every
in-process tool so external MCP clients (Claude Desktop, web app) get
the same affordances Hikari's own runtime already enforces via
``access_mode`` in ``config/tools.yaml``.

The annotations are HINTS only — never gates. Runtime enforcement still
flows through ``tools/_tools_yaml.py`` (access_mode + gate).

Mapping rules (from plan 6F):
  Read-only             → readOnlyHint=True
  Write (DB/disk)       → readOnlyHint=False, destructiveHint=False
  Destructive (deletes) → readOnlyHint=False, destructiveHint=True
  External IO (network) → openWorldHint=True; otherwise False

Six combinations, named below. Each in-process tool maps to exactly one
constant via ``ANNOTATIONS_BY_TOOL`` — a single source of truth that the
test harness cross-checks against ``config/tools.yaml``'s ``access_mode``.
"""

from __future__ import annotations

from mcp.types import ToolAnnotations

# --- six canonical annotation profiles ---

ANN_READ_LOCAL = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
ANN_READ_EXTERNAL = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
ANN_WRITE_LOCAL = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, openWorldHint=False,
)
ANN_WRITE_EXTERNAL = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, openWorldHint=True,
)
ANN_DESTRUCTIVE_LOCAL = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, openWorldHint=False,
)
ANN_DESTRUCTIVE_EXTERNAL = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, openWorldHint=True,
)

# --- per-tool mapping (single source of truth) ---
#
# Names match the @tool(name, ...) first arg exactly. The test in
# ``tests/test_tool_annotations.py`` asserts:
#   1. Every in-process registered tool has an entry here.
#   2. The annotation matches the registry's access_mode classification.
#
# When adding a new @tool, add its entry below.

ANNOTATIONS_BY_TOOL: dict[str, ToolAnnotations] = {
    # --- memory (DB) ---
    "recall": ANN_READ_LOCAL,
    "session_search": ANN_READ_LOCAL,
    "remember": ANN_WRITE_LOCAL,
    # mark_fact_invalid taints a memory row so recall stops surfacing it —
    # operationally indistinguishable from delete from the model's view.
    "mark_fact_invalid": ANN_DESTRUCTIVE_LOCAL,
    "task_create": ANN_WRITE_LOCAL,
    "task_update": ANN_WRITE_LOCAL,
    # update_core_block overwrites identity-shaping prompt content with no
    # schema-level history of the prior value — treat as destructive write.
    "update_core_block": ANN_DESTRUCTIVE_LOCAL,
    # --- wiki (filesystem) ---
    "wiki_read": ANN_READ_LOCAL,
    "wiki_list": ANN_READ_LOCAL,
    "wiki_search": ANN_READ_LOCAL,
    "wiki_backlinks": ANN_READ_LOCAL,
    "wiki_tree": ANN_READ_LOCAL,
    "wiki_append": ANN_WRITE_LOCAL,
    "morning_brief": ANN_READ_LOCAL,
    # --- attachments (filesystem) ---
    "read_attachment": ANN_READ_LOCAL,
    # --- calc / python_run (pure compute) ---
    "calc": ANN_READ_LOCAL,
    "python_run": ANN_READ_LOCAL,
    # --- router (in-memory BM25) ---
    "tool_search": ANN_READ_LOCAL,
    # --- day_receipt (DB) ---
    "receipt_get": ANN_READ_LOCAL,
    "receipt_today": ANN_READ_LOCAL,
    "receipt_week": ANN_READ_LOCAL,
    "receipt_read": ANN_READ_LOCAL,
    "receipt_search": ANN_READ_LOCAL,
    "receipt_print": ANN_READ_LOCAL,
    "receipt_add": ANN_WRITE_LOCAL,
    "receipt_set_note": ANN_WRITE_LOCAL,
    "receipt_delete": ANN_DESTRUCTIVE_LOCAL,
    # --- reminders (DB + scheduler) ---
    "reminder_list": ANN_READ_LOCAL,
    "reminder_create": ANN_WRITE_LOCAL,
    "reminder_snooze": ANN_WRITE_LOCAL,
    "reminder_cancel": ANN_DESTRUCTIVE_LOCAL,
    "accountability_create": ANN_WRITE_LOCAL,
    "accountability_resolve": ANN_WRITE_LOCAL,
    # --- decision_log (DB) ---
    "decision_log_capture": ANN_WRITE_LOCAL,
    "decision_log_resolve": ANN_WRITE_LOCAL,
    # --- diary (DB read) ---
    "diary_read": ANN_READ_LOCAL,
    # --- jobhunt (Sprint 2; local sqlite/markdown reads) ---
    "jobhunt_radar": ANN_READ_LOCAL,
    "jobhunt_org": ANN_READ_LOCAL,
    "jobhunt_prep": ANN_READ_LOCAL,
    # jobhunt_draft_touch composes via LLM then creates a Gmail draft
    # (external network write, never sends) — same profile as link_save.
    "jobhunt_draft_touch": ANN_WRITE_EXTERNAL,
    # --- controls: runtime-state switches ---
    # set_silence writes silence_until — local state, not destructive.
    "set_silence": ANN_WRITE_LOCAL,
    # set_proactive_source writes proactive_enabled_sources_override /
    # proactive_snooze_until — local state, not destructive.
    "set_proactive_source": ANN_WRITE_LOCAL,
    # checkin_control writes runtime flags and schedule skip_dates —
    # local state, not destructive.
    "checkin_control": ANN_WRITE_LOCAL,
    # capabilities_overview reads the tool catalog + command menu config —
    # pure local read, no side effects.
    "capabilities_overview": ANN_READ_LOCAL,
    # --- apple_notes (local osascript; iCloud sync is async/out-of-band) ---
    "note_read": ANN_READ_LOCAL,
    "note_search": ANN_READ_LOCAL,
    "note_create": ANN_WRITE_LOCAL,
    # --- calendar (external read) ---
    # sync_apple_reminder and sync_gcal_reminder are removed: they are
    # scheduler-internal callers only (not LLM-reachable @tools).
    "calendar_get_events": ANN_READ_EXTERNAL,
    # --- gmail (external read; typed adapter, replaces drive_gmail delegation) ---
    "query_inbox": ANN_READ_EXTERNAL,
    # --- external read APIs ---
    "arxiv_search": ANN_READ_EXTERNAL,
    "currency_convert": ANN_READ_EXTERNAL,
    "translate": ANN_READ_EXTERNAL,
    "weather_fetch": ANN_READ_EXTERNAL,
    "places_search": ANN_READ_EXTERNAL,
    "place_open_now": ANN_READ_EXTERNAL,
    "ytmusic_search": ANN_READ_EXTERNAL,
    "ytmusic_library": ANN_READ_EXTERNAL,
    "ytmusic_recent": ANN_READ_EXTERNAL,
    # --- external write / dispatch ---
    # dispatch_claude_session can be granted Bash / Edit / Write — arbitrary
    # host command execution on the operator's machine. Surface that as
    # destructive in external MCP clients so a single-click approval has
    # the same friction as a delete.
    "dispatch_claude_session": ANN_DESTRUCTIVE_EXTERNAL,
    # --- link_shelf (DB write + HTTP fetch for metadata) ---
    "link_save": ANN_WRITE_EXTERNAL,
    "link_search": ANN_READ_LOCAL,
    "link_list": ANN_READ_LOCAL,
    "link_update": ANN_WRITE_LOCAL,
    "link_delete": ANN_DESTRUCTIVE_LOCAL,
    # --- skill management (read/write local .agents/skills/ + DB) ---
    "skill_list": ANN_READ_LOCAL,
    "skill_read": ANN_READ_LOCAL,
    "skill_create": ANN_WRITE_LOCAL,
    "skill_approve": ANN_WRITE_LOCAL,
    # run_skill executes arbitrary skill content which may call external tools
    "run_skill": ANN_WRITE_EXTERNAL,
    # --- runtime progress signalling (Telegram send — external write) ---
    "progress": ANN_WRITE_EXTERNAL,
    # --- voice outbound (ElevenLabs TTS → Telegram send — external write) ---
    "voice_outbound_send": ANN_WRITE_EXTERNAL,
}


def annotations_for(tool_name: str) -> ToolAnnotations | None:
    """Return the ToolAnnotations for an in-process tool name, or None
    if unmapped. Returning None at the @tool decorator site means the SDK
    omits annotations for that tool (back-compat with pre-6F state)."""
    return ANNOTATIONS_BY_TOOL.get(tool_name)
