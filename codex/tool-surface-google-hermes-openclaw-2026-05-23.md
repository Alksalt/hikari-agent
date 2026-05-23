---
title: Tool Surface Research - Google, Hermes, and OpenClaw
date: 2026-05-23
repo: /Users/ol/agents/hikari-agent
reviewer: Codex
---

# Tool Surface Research - Google, Hermes, and OpenClaw

## Executive Take

Hikari already has the right core shape: a small always-on companion with
personal memory, Telegram delivery, Google Workspace, Notion, GitHub, browser,
Apple-local tools, wiki, reminders, music, places, weather, arXiv, attachments,
and a codex-report reader. The next tool work should not be "add every MCP".
It should be:

1. **Tighten Google write gating before adding broader Google coverage.**
2. **Add a Google Workspace CLI (`gws`) skill/CLI path as a supplemental surface,
   not as a replacement for the current Google Workspace MCP yet.**
3. **Borrow Hermes/OpenClaw patterns for scheduled routines, background coding
   workers, and tool-call observability only where they fit Hikari's single-user
   companion shape.**
4. **Prefer a few high-signal personal tools over broad platform sprawl.**

The most attractive new Google capabilities are Google Tasks, People/Contacts,
Workspace Events, and `gws` helper workflows such as agenda, meeting prep,
weekly digest, email-to-task, Drive upload, and Sheets append/read.

## Local Inventory

### Core Runtime and Tool Registry

Current architecture is registry-driven:

- `config/tools.yaml` defines MCP servers, external packages, gating,
  untrusted-output wrapping, and subagent tool grants.
- `tools/_registry.py` auto-discovers utility tools from feature folders.
- `agents/runtime.py` builds allowed tools from the YAML registry plus the
  utility registry.
- `agents/tool_inventory.py` injects a per-turn tool inventory so Hikari does
  not hallucinate missing or blocked tools.

Current always-on in-process families:

- `hikari_memory`: recall, remember, invalidation, core-block update, task
  create/update.
- `hikari_photo`: photo generation.
- `hikari_wiki`: search/read/append/backlinks/list/tree.
- `hikari_dispatch`: background Claude-session dispatch plus post-approval
  confirmed execution.
- `hikari_codex`: list/read reports from `codex/`.
- `hikari_utility`: reminders, currency, day receipt, attachments, weather,
  Apple Notes, decision log, calc/python, arXiv, places, YouTube Music,
  translation.

Current external MCP families:

- Google Workspace via `uvx --from google-workspace-mcp`.
- Notion via `@notionhq/notion-mcp-server`.
- GitHub via `@modelcontextprotocol/server-github`.
- Playwright MCP.
- Apple Events MCP.
- Apple Shortcuts MCP.
- YouTube Transcript MCP.
- DuckDB/MotherDuck MCP.

Current subagents:

- `wiki`, `drive_gmail`, `notion`, `research`, `github`.

### Current Google Surface

The `drive_gmail` specialist prompt lists these Google Workspace operations as
real `google-workspace-mcp` exports:

- Calendar: get event(s), create event, delete event.
- Gmail read: search/query, message details, attachment content.
- Gmail write: create/delete draft, send draft, send email, reply, bulk delete.
- Drive: search/read/upload/create folder/delete/list shared drives.
- Docs: create/read metadata/read markdown/append/prepend/insert/batch update/
  insert image.
- Sheets: create/read/write/append/clear/add sheet/delete sheet.
- Slides: read/create/duplicate/delete and add text/table/bullets/notes.

Gated Google tools in `config/tools.yaml` are narrower:

- `gmail_send_email`, `gmail_reply_to_email`, `gmail_bulk_delete_messages`.
- `delete_calendar_event`, `create_calendar_event`.
- `drive_delete_file`, `drive_delete_folder`, `drive_upload_file`.

Everything else falls through the wildcard `mcp__google_workspace__*` with
`gate: null` and untrusted-output wrapping.

That means these likely writes are currently not explicitly gated:

- `create_gmail_draft`, `delete_gmail_draft`, `gmail_send_draft`.
- `drive_create_folder`.
- Docs writes and document creation.
- Sheets writes, clears, creation, add/delete sheet.
- Slides creation, mutation, duplication, deletion.

This is the biggest local finding. Before adding broader Google capability,
make the Google write matrix explicit and tested.

## Hermes and OpenClaw Comparison

### Hermes

Hermes' official docs describe a broad toolset: web search/extract, terminal,
files, browser automation, media generation/analysis, planning/delegation,
memory/session search, cron jobs, messaging delivery, Home Assistant, MCP,
Spotify, Discord, Feishu, Yuanbao, RL tools, and more. Hermes also supports MCP
servers with per-server filtering and automatic discovery.

The Hermes Google Workspace skill is especially relevant. It currently covers
Gmail, Calendar, Drive, Contacts, Sheets, and Docs through Hermes-managed OAuth
and a thin CLI wrapper; when `gws` is installed, it prefers `gws` as the
backend, otherwise it falls back to bundled Python scripts. A Hermes issue also
lays out the intended migration path: replace custom Python Google scripts with
`gws`, then maybe use a `gws`-backed MCP server later.

What to borrow:

- Skill-first Google workflows: teach the agent when to use Google tools and
  workflows, not just expose more raw APIs.
- Session search as a first-class tool. Hikari has memory recall, but not a
  user-facing "search past sessions" surface beyond SQLite/DuckDB.
- Cron/scheduled task ergonomics. Hikari has APScheduler jobs, but Hermes'
  user-facing cron tool is a useful pattern for owner-created routines.
- Better messaging surfaces only if needed. Hermes supports many platforms;
  Hikari should stay Telegram-first unless a concrete workflow needs another
  surface.

What not to borrow:

- Broad tool bloat. Hikari's personality and safety depend on a smaller,
  better-gated surface.
- Terminal/file tools for the main companion path. Hikari already has controlled
  dispatch and Codex reports; direct shell would expand blast radius.

Sources:

- Hermes tools overview: https://hermes-agent.nousresearch.com/docs/user-guide/features/tools/
- Hermes built-in tools reference: https://hermes-agent.nousresearch.com/docs/reference/tools-reference
- Hermes MCP docs: https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp
- Hermes Google Workspace skill: https://github.com/NousResearch/hermes-agent/blob/main/skills/productivity/google-workspace/SKILL.md
- Hermes `gws` migration issue: https://github.com/NousResearch/hermes-agent/issues/411

### OpenClaw

No local OpenClaw checkout was present under `/Users/ol`; this comparison uses
public OpenClaw sources plus existing local memory that OpenClaw was an earlier
agent experiment and is now abandoned for this project.

OpenClaw's public control-plane page emphasizes:

- Telegram access.
- Claude Code sessions in sandboxed containers.
- Hermes as a second agent.
- Scheduled morning briefs: Hacker News, world news, weather, stocks/crypto,
  with Reddit and GitHub trending planned.
- Semantic search via `/learn`.
- Dashboard/REST CRUD for routines.
- Tool-call audit trails.
- Docker health checks, secrets mode `600`, Cloudflare/nginx, no telemetry.

The OpenClaw `coding-agent` skill is also relevant. It delegates to Codex,
Claude Code, OpenCode, or Pi as background workers, requires a real notification
route, and requires workers to send a completion/failure message rather than
depending on a heartbeat.

What to borrow:

- Routine types: HN/news/stocks/GitHub-trending/Reddit as optional morning
  brief modules. Hikari already has morning weather and AI briefings in the
  wiki, but not a generic owner-configurable brief registry.
- Tool-call audit UX. Hikari logs tool use internally; a compact `/tools` or
  `/audit` Telegram command could make failures and side effects easier to see.
- Background coding worker discipline: explicit notification routes, process
  monitoring, no silent fallback, issue-to-PR loop shape.
- Container isolation only for powerful worker sessions, not for normal chat.

What not to borrow:

- YOLO-mode agent operation. Hikari's current approval/gatekeeper model is a
  better match for a private companion with email, docs, and memory access.
- A full dashboard before the Telegram UX asks for it.

Sources:

- OpenClaw control-plane page: https://claw.biswas.me/
- OpenClaw coding-agent skill: https://github.com/openclaw/openclaw/blob/main/skills/coding-agent/SKILL.md

## Google Research

### `gws` Is Interesting, But Current MCP Status Is Mixed

Google's `googleworkspace/cli` is a Rust CLI for Workspace APIs. Current README
features include OAuth setup, structured JSON output, generated command trees
from Discovery documents, helper commands, agent skills, OpenClaw skill setup,
Gemini CLI extension support, pagination, uploads, and Model Armor response
sanitization.

Important version caveat:

- `gws` added a `gws mcp` command in `0.3.0`.
- `gws` added compact/full tool modes in `0.5.0`.
- The changelog says the `mcp` command was removed in `0.8.0`.
- Current changelog top is `0.22.5`, and the current README no longer presents
  `gws mcp` as the primary path.

So the direct recommendation is not "replace `google-workspace-mcp` with
official `gws mcp`" today. The safer path is:

- Keep the existing Google Workspace MCP for direct Claude tool calls.
- Add `gws` as a CLI-backed skill or internal utility for missing services and
  helper workflows.
- Re-evaluate a gws-backed MCP wrapper separately if the wrapper is mature and
  keeps a compact tool surface.

Sources:

- Google Workspace CLI README: https://github.com/googleworkspace/cli
- Google Workspace CLI changelog: https://github.com/googleworkspace/cli/blob/main/CHANGELOG.md
- Google Workspace CLI / OpenClaw setup lines in README: https://github.com/googleworkspace/cli
- gws-backed MCP wrapper example: https://github.com/aaronsb/google-workspace-mcp

### Google Capabilities Worth Adding

Priority 1 - **Google write-gating audit**

Make every Google write/send/delete/mutate tool explicit in `config/tools.yaml`
before broadening scope. Include tests for Docs, Sheets, Slides, drafts,
send-draft, Drive folder creation, Drive folder delete, and any future Tasks or
People writes.

Priority 2 - **Google Tasks**

Google Tasks is a natural match for Hikari's reminders/open-loops layer. It
would let Hikari mirror or read task lists alongside Apple Reminders and local
SQLite reminders. Caveat: keep local reminders authoritative unless the user
explicitly wants Google Tasks to become the shared task surface.

Source: https://developers.google.com/workspace/tasks/reference/rest

Priority 3 - **People/Contacts read**

Contacts make email/calendar workflows less brittle: "email Maria", "who is
this?", "find the person from the meeting", "use the right address". Start
read-only. Defer contact creation/update/delete until there is a real need.

Source: https://developers.google.com/people/api/rest/v1/people.connections/list

Priority 4 - **Workspace Events / Gmail watch**

Hikari's proactive system would benefit from event-driven inputs instead of
polling everything. Workspace Events can subscribe to supported Workspace
events; `gws` helper docs also mention event subscription/renewal helpers. This
is promising for "important mail arrived", Drive file changes, or meeting
state changes, but should be piloted carefully because event streams can become
noisy and expensive.

Source: https://developers.google.com/workspace/events

Priority 5 - **Model Armor as optional sanitizer**

Hikari already wraps untrusted outputs in explicit delimiters. Model Armor could
be useful as a second scanner for Gmail/Docs/Drive content, especially if using
`gws` because the CLI has documented `modelarmor` helpers and sanitize env vars.
Do not replace the local delimiter policy with Model Armor; treat it as an
optional defense-in-depth layer.

Sources:

- Google Cloud Model Armor sanitize docs: https://docs.cloud.google.com/security-command-center/docs/sanitize-prompts-responses
- Google Workspace CLI Model Armor README section: https://github.com/googleworkspace/cli

Priority 6 - **Google helper workflows**

These are high leverage because they compress multi-step routines into named
operations:

- `gmail +triage`, `+send`, `+reply`, `+watch`.
- `calendar +agenda`, `+insert`.
- `drive +upload`.
- `sheets +read`, `+append`.
- `docs +write`.
- `workflow +standup-report`, `+meeting-prep`, `+email-to-task`,
  `+weekly-digest`, `+file-announce`.

For Hikari, these should become a `gws` skill/reference and maybe a small
wrapper for selected helpers, not a raw firehose of hundreds of Google methods.

Source: https://github.com/googleworkspace/cli

## Candidate Tool Roadmap

### Add Soon

1. **Google Workspace write-gating expansion**
   - Not a new user-facing tool, but required safety work.
   - Add explicit YAML entries and tests for every known Google write/mutation.

2. **Google Tasks read + optional mirror**
   - Tools: list task lists, list tasks, create task, complete task.
   - Gate writes; reads untrusted-wrapped.
   - Decide relationship with local reminders before syncing.

3. **Google People/Contacts read-only**
   - Tools: search/list contacts, maybe contact detail.
   - No contact mutation initially.

4. **Session search / transcript search**
   - Could be backed by existing SQLite/DuckDB.
   - User-facing command: "what did we say about X last week?"
   - This is Hermes-inspired and fits Hikari better than adding another app.

5. **Owner-configurable brief sources**
   - HN, general news, stocks/crypto, GitHub trending, Reddit.
   - Start as read-only routine producers with explicit source attribution.
   - The wiki already stores briefings; connect the loop cleanly.

### Add Later / Conditional

6. **`gws` CLI skill**
   - Use it for missing Workspace surfaces and helper workflows.
   - Do not expose all generated commands to chat.
   - Consider a compact wrapper with allowlisted helper commands.

7. **Workspace Events**
   - Use for proactive triggers once core polling/routines are stable.
   - Needs dedupe, rate limits, topic/subscription lifecycle, and quiet-hours
     integration.

8. **Readwise / Reader**
   - README mentions `READWISE_TOKEN`, but no local tool exists in the current
     inventory.
   - Worth adding if the user wants saved highlights surfaced in memory/briefs.

9. **Finance/watchlist**
   - OpenClaw has stocks/crypto briefs. Add only if the user wants daily market
     checks; otherwise it is noise.

10. **Slack/Discord/Email inbound surfaces**
    - Hermes supports many surfaces, but Hikari should stay Telegram-first.
    - Add another surface only when it is tied to a specific workflow.

### Avoid For Now

- Full `gws`/Google API firehose.
- Direct shell/file tools in the main Hikari chat loop.
- Full OpenClaw-style dashboard.
- Multi-VM/fleet/plugin marketplace work.
- Broad social media tools unless the user asks for a concrete content
  workflow.

## Implementation Notes

### Google Gating Matrix

Add explicit entries for likely Google write tools:

- Gmail: `create_gmail_draft`, `delete_gmail_draft`, `gmail_send_draft`.
- Drive: `drive_create_folder`.
- Docs: `docs_create_document`, `docs_append_text`, `docs_prepend_text`,
  `docs_insert_text`, `docs_batch_update`, `docs_insert_image`.
- Sheets: `sheets_create_spreadsheet`, `sheets_write_range`,
  `sheets_append_rows`, `sheets_clear_range`, `sheets_add_sheet`,
  `sheets_delete_sheet`.
- Slides: `create_presentation`, `create_slide`, `add_text_to_slide`,
  `add_formatted_text_to_slide`, `add_bulleted_list_to_slide`,
  `add_table_to_slide`, `add_slide_notes`, `duplicate_slide`, `delete_slide`,
  `create_presentation_from_markdown`.

Suggested policy:

- Sends, deletes, clears, and share/permission changes: gatekeeper/defer.
- Draft creation and non-destructive document append: maybe defer, not
  gatekeeper, depending on how much friction is acceptable.
- Reads: no gate, but always untrusted-wrapped.

### `gws` Integration Shape

Start with one of these:

1. **Skill-only**: add a Hikari skill that teaches when/how to use `gws`, but
   only available to dispatched coding/control contexts that can run shell.
2. **Narrow utility wrapper**: implement `gws_helper` with an enum of approved
   helpers (`gmail_triage`, `calendar_agenda`, `workflow_weekly_digest`,
   `workflow_meeting_prep`, etc.).
3. **External MCP wrapper**: evaluate a gws-backed MCP that exposes compact
   curated tools instead of hundreds of generated methods.

For Hikari, option 2 is probably the best eventual product shape. Option 1 is
fastest. Option 3 needs more security review.

## Bottom Line

The best next tool is not another mega-connector. It is a safer Google layer:
explicit Google write gates, then Tasks/Contacts, then selected `gws` workflows.
After that, the best Hermes/OpenClaw import is a polished routines surface:
owner-configurable brief sources, transcript/session search, and visible
tool-call/audit status.

Hikari should stay small enough to trust. The trick is giving her sharper hands,
not more hands.
