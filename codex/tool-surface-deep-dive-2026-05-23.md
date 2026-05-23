---
title: Tool Surface Deep Dive
date: 2026-05-23
repo: /Users/ol/agents/hikari-agent
reviewer: Codex
supersedes_or_extends: codex/tool-surface-google-hermes-openclaw-2026-05-23.md
---

# Tool Surface Deep Dive

## Why This Exists

The first memo answered "what should we maybe add?" This pass asks the harder
question: what tool surface should Hikari trust enough to carry in a live,
single-user companion that can read memory, email, calendar, docs, and private
files?

The answer is narrower than the catalog suggests.

The right next step is not a bigger tool list. The right next step is a better
tool contract:

1. Explicit Google write gates.
2. Smaller Google helpers with stable semantics.
3. Session/tool audit surfaces so the user can see what happened.
4. Event/routine tools only after dedupe, quiet hours, and lifecycle handling.
5. `gws` used as a curated backend or skill reference, not exposed raw.

## Deep Findings

### F1 - The Google Workspace write surface is currently under-specified

Hikari's `drive_gmail` prompt lists many real Google Workspace operations:
Gmail drafts/sends, Calendar create/delete, Drive upload/create/delete, Docs
writes, Sheets writes/clears, and Slides mutation. `config/tools.yaml` only
gates a subset:

- Gmail send/reply/bulk-delete.
- Calendar create/delete.
- Drive delete/upload.

The wildcard `mcp__google_workspace__*` catches the rest with `gate: null`.
That is fine for reads, but it is too loose for hidden writes such as:

- `create_gmail_draft`, `delete_gmail_draft`, `gmail_send_draft`.
- `drive_create_folder`.
- `docs_create_document`, `docs_append_text`, `docs_prepend_text`,
  `docs_insert_text`, `docs_batch_update`, `docs_insert_image`.
- `sheets_create_spreadsheet`, `sheets_write_range`, `sheets_append_rows`,
  `sheets_clear_range`, `sheets_add_sheet`, `sheets_delete_sheet`.
- `create_presentation`, `create_slide`, `duplicate_slide`, `delete_slide`,
  `create_presentation_from_markdown`, and slide content mutators.

Recommendation: before any Google expansion, add explicit tool entries and
tests for all known write-like Google Workspace exports.

Suggested policy:

| Category | Examples | Gate |
|---|---|---|
| Send/outbound | `gmail_send_email`, `gmail_reply_to_email`, `gmail_send_draft`, Chat sends | `gatekeeper` or `defer` |
| Destructive | delete mail/drafts/files/folders/events/slides/sheets, clear ranges | `gatekeeper` |
| Calendar mutations | create/update/delete events, attendee changes | `defer` minimum |
| Document mutations | append/insert/batch update Docs, Sheets, Slides | `defer` by default |
| Draft creation | create draft email | `defer` if recipient/body non-empty; otherwise allow only through a dedicated draft helper |
| Drive organization | create folder, move/copy/share | `defer` |
| Reads | Gmail/Drive/Docs/Sheets/Calendar read | no gate, but untrusted-wrapped |

Why gate drafts? Drafts are not sent, but they are still private mutable state,
can leak content into Gmail, and can be used by follow-up send tools. Auto-run
drafts are acceptable only through a constrained helper that makes the side
effect obvious.

### F2 - `gws` is useful, but not as a raw runtime connector

The Google Workspace CLI (`gws`) is attractive because it gives structured JSON,
auto-pagination, `--dry-run`, schema introspection, generated commands from
Discovery documents, helper commands, and agent skills.

But it is not a clean replacement for the current MCP server today:

- Its README says it is not an officially supported Google product.
- It is pre-1.0 and explicitly warns to expect breaking changes.
- The changelog shows rapid change through `0.22.5`.
- The early `gws mcp` command existed historically, but the current README
  positions `gws` as CLI plus skills/extensions, not native MCP.
- Shelling out to a giant CLI means Hikari's permission model needs to wrap the
  operation before execution, not rely on the CLI's own skill prose.

Use `gws` as one of these, in this order:

1. A source of skill patterns and helper names.
2. A backend for a small `gws_helper` wrapper with an enum of approved helpers.
3. A backend inside background worker contexts, not the main live chat loop.
4. A future MCP backend only if a compact wrapper is stable and auditable.

Do not expose the full generated command tree to Hikari.

Key `gws` affordances worth stealing:

- `--dry-run` before writes.
- `--page-all` NDJSON pagination for large reads.
- `gws schema <method>` for tool design and request preview.
- Time-aware helpers such as agenda, meeting prep, weekly digest.
- `+watch` and `events +subscribe` long-running streams, but only in supervised
  background jobs.
- Model Armor sanitize hooks as optional defense in depth.

Sources:

- `gws` README: https://github.com/googleworkspace/cli
- `gws` changelog: https://github.com/googleworkspace/cli/blob/main/CHANGELOG.md
- `gws` skills index: https://github.com/googleworkspace/cli/blob/main/docs/skills.md
- `gws-shared` skill: https://github.com/googleworkspace/cli/blob/main/skills/gws-shared/SKILL.md

### F3 - Google's official MCP is currently documentation-oriented, not account-control

Google Workspace Developer Tools documents a Workspace Developer MCP server, but
the documented use case is official developer documentation and snippets, not
direct Gmail/Drive/Calendar account control. The same page notes that an MCP
server connecting to Workspace APIs is still a feature-request path.

This matters because "Google has an MCP" does not currently mean "replace our
Google Workspace MCP with Google's account-control MCP."

Recommendation:

- Add the official `workspace-developer` MCP only for research/build assistance
  if Hikari starts building Workspace API integrations often.
- Do not put it in the user-facing companion path unless there is a concrete
  API-development workflow.

Source:

- Google Workspace Developer Tools: https://developers.google.com/workspace/guides/developer-tools

### F4 - The best missing Google tools are Tasks and People, not more Docs/Slides power

Google Tasks and Google People/Contacts are high leverage because they improve
Hikari's existing behavior:

- Tasks maps to reminders, open loops, and daily planning.
- People maps to email/calendar disambiguation.

Docs/Sheets/Slides write power is useful, but it mostly expands blast radius
unless attached to a specific workflow.

#### Google Tasks

Google Tasks exposes tasklists and tasks. Task methods include list/get/insert,
patch/update, move, delete, and clear completed tasks. The task JSON resource
includes title, notes, status, due, completed, parent/position, links, and a
web view link.

Suggested first tools:

- `google_tasks_list_lists()`
- `google_tasks_list(tasklist_id, show_completed=false, due_before=null)`
- `google_tasks_create(tasklist_id, title, notes=null, due=null)`
- `google_tasks_complete(tasklist_id, task_id)`

Gate:

- reads: no gate, wrapped.
- create/complete/move/update/delete/clear: `defer`.
- delete/clear completed: `gatekeeper`.

Important product choice:

- Local Hikari reminders should remain authoritative initially.
- Google Tasks should be a mirror/import/export target until the user chooses
  otherwise.

Sources:

- Tasks API overview: https://developers.google.com/workspace/tasks/reference/rest
- Tasks resource: https://developers.google.com/workspace/tasks/reference/rest/v1/tasks

#### Google People/Contacts

People `people.connections.list` provides paginated contacts, supports
field masks, page tokens, and sync tokens. Sync tokens expire after 7 days,
and full-sync first pages have special quota behavior. Read-only contacts need
`contacts.readonly`; write access needs broader contact scopes.

Suggested first tools:

- `google_contacts_search(query, limit=10)`
- `google_contacts_list_recently_changed(limit=50, sync_token=null)`
- `google_contact_get(resource_name, person_fields="names,emailAddresses,phoneNumbers,organizations")`

Gate:

- read-only only at first.
- no contact create/update/delete until a specific workflow needs it.

Use cases:

- "email Maria" disambiguation.
- match meeting attendees to known people.
- resolve sender identities before a reply.
- avoid hallucinating addresses from memory.

Source:

- People connections list: https://developers.google.com/people/api/rest/v1/people.connections/list

### F5 - Push/event systems are powerful but operationally expensive

Gmail push notifications use Pub/Sub, `users.watch`, mailbox `historyId`, and
`history.list`. Watches must be renewed at least every 7 days; Google recommends
calling `watch` once per day. Notifications include mailbox/history metadata,
not the full mail body, so Hikari still has to fetch and dedupe.

Workspace Events supports subscriptions for Chat, Drive, and Meet resources.
Subscriptions have target resources, event types, payload options, Pub/Sub
notification endpoints, state, suspension reasons, expiration, renew/reactivate
methods, and lifecycle events.

This is not a small "add a tool" feature. It needs a local event subsystem:

- `external_events` table with source, resource, remote id, cursor/history id,
  dedupe key, state, received_at, processed_at.
- renewal scheduler.
- quiet-hours integration.
- "interestingness" scoring before a proactive message.
- failure/revocation handling.
- strict no-body-in-event assumptions for Gmail.

Recommendation:

- Do not add event push first.
- Start with polling plus explicit user asks.
- Add Gmail watch only when inbox proactive behavior is truly needed.
- Add Workspace Events only for Drive/Meet use cases where polling is bad.

Sources:

- Gmail push notifications: https://developers.google.com/workspace/gmail/api/guides/push
- Gmail `users.watch`: https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.watch
- Workspace Events overview: https://developers.google.com/workspace/events
- Workspace subscriptions resource: https://developers.google.com/workspace/events/reference/rest/v1/subscriptions

### F6 - Hermes is strongest as a product-pattern source, not a tool list to copy

Hermes has a broad tool registry: web, search, terminal, file, browser, vision,
image, TTS, memory, session search, cron, delegation, messaging, Home Assistant,
Spotify, Discord, and MCP. Hikari should not copy that breadth.

The valuable Hermes patterns are:

1. **Session search**
   - Hermes stores sessions and exposes cross-session search.
   - Hikari has messages/facts/episodes and DuckDB docs, but no clean
     user-facing transcript search.

2. **Cron as an agent tool**
   - Hermes exposes a `cronjob` tool that can schedule, pause, edit, trigger,
     remove, attach skills, and deliver to targets.
   - Hikari has APScheduler plus config, but user-created recurring routines
     are less first-class.

3. **MCP filtering**
   - Hermes emphasizes per-server tool filtering.
   - Hikari already has YAML wildcard/explicit precedence; the missing piece is
     stronger policy tests for wildcard-expanded writes.

4. **Messaging/event bridge**
   - Hermes MCP can read sessions/messages and poll events.
   - Hikari has Telegram bridge and post-send persistence; a read-only
     introspection tool could expose recent tool calls and approvals without a
     separate dashboard.

5. **Google skill ergonomics**
   - Hermes' Google docs are workflow-first: search, read, send, list events,
     create event, search Drive, read/write Sheets, get Docs, contacts.
   - Hikari's Google subagent prompt has operation names, but fewer workflow
     recipes.

Sources:

- Hermes tools: https://hermes-agent.nousresearch.com/docs/user-guide/features/tools/
- Hermes built-in tools reference: https://hermes-agent.nousresearch.com/docs/reference/tools-reference/
- Hermes memory: https://hermes-agent.nousresearch.com/docs/user-guide/features/memory/
- Hermes sessions: https://hermes-agent.nousresearch.com/docs/user-guide/sessions/
- Hermes cron: https://hermes-agent.nousresearch.com/docs/user-guide/features/cron/
- Hermes MCP: https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp
- Hermes Google Workspace skill docs: https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/skills/google-workspace.md
- Hermes `gws` migration issue: https://github.com/NousResearch/hermes-agent/issues/411

### F7 - OpenClaw's most useful idea is worker discipline

OpenClaw's coding-agent skill is strict about background workers:

- spawn immediately in background;
- use the right PTY/non-PTY mode per agent;
- monitor with process tools;
- require direct completion notification;
- do not silently take over if the worker fails.

Hikari already has `dispatch_claude_session`, background task rows, and a
Telegram owner route. The useful import is not OpenClaw's whole control plane.
It is the worker contract:

- every dispatched worker should have a notification route;
- every worker should have a terminal state surfaced to Telegram or `/tasks`;
- no worker should rely on a heartbeat to maybe mention completion;
- the audit log should show spawn, started, last output, completion/failure,
  and any produced artifact.

OpenClaw's routine set is also relevant: morning brief modules for HN, world
news, weather, stocks/crypto, GitHub trending, Reddit. Hikari should only add
these as explicit opt-in brief sources, not ambient noise.

Sources:

- OpenClaw page: https://claw.biswas.me/
- OpenClaw coding-agent skill: https://github.com/openclaw/openclaw/blob/main/skills/coding-agent/SKILL.md
- OpenClaw skills docs: https://github.com/openclaw/openclaw/blob/main/docs/tools/skills.md

## Provider Options For Google

| Option | Strength | Risk | Recommendation |
|---|---|---|---|
| Current `google-workspace-mcp` | Already wired, direct MCP, known subagent prompt | Package surface may drift; only some writes gated; launch via floating `uvx` | Keep, but pin/audit and gate all writes |
| `gws` CLI wrapper | Broad API coverage, JSON, dry-run, auto-pagination, skills, recipes | pre-1.0, not officially supported, shell wrapper risk, huge command surface | Use only behind narrow helpers |
| Google developer MCP | Official docs/snippets | Not account-control API access | Add only for development assistance |
| Direct Google REST tools | Precise, small, testable, first-class gates | Custom code/maintenance | Best for Tasks/Contacts MVP |
| Third-party Workspace MCPs | Wide service coverage, sometimes OAuth2.1/multi-user | More trust surface, larger blast radius, unknown release discipline | Study, do not switch blindly |

## Recommended Tool Roadmap

### P0 - Safety Patch Before Expansion

Goal: no unknown Google writes through the wildcard.

Tasks:

1. Add explicit `config/tools.yaml` entries for every known Google write-like
   tool from the current `drive_gmail` prompt.
2. Add `config/scopes.yaml` entries for every explicit Google write tool.
3. Expand `tests/test_google_workspace_send_policy.py` or create
   `tests/test_google_workspace_write_policy.py`.
4. Add tests proving all listed writes are either `defer` or `gatekeeper`.
5. Add a test proving representative reads still pass and are wrapped.
6. Update `agents/subagents/prompts/drive_gmail.prompt.md` so it does not claim
   "drafts auto-run" if we decide to gate them.

Acceptance:

- `load_registry().validate()` clean.
- Every write-like Google tool has an explicit spec.
- The wildcard is only relied on for reads/unknowns, not known writes.

### P1 - Contact Read Tools

Goal: eliminate email/contact hallucination and improve person disambiguation.

Implementation shape:

- New in-process feature folder: `tools/google_contacts/`.
- Use the existing Google OAuth provider/token refresh path rather than a new
  credential store if possible.
- Read-only scope: `https://www.googleapis.com/auth/contacts.readonly`.
- Wrap outputs as untrusted.
- Cache sync token metadata in `runtime_state` or a small feature table.

Candidate tools:

```text
google_contacts_search(query: str, limit: int = 10)
google_contact_get(resource_name: str, person_fields: str = "names,emailAddresses,phoneNumbers,organizations")
google_contacts_recent(limit: int = 50)
```

Do not implement contact writes yet.

### P1 - Session Search

Goal: give Hikari an honest way to answer "what did we say about X?" without
conflating curated facts with raw transcript memory.

Implementation shape:

- New in-process tool under `tools/session_search/` or extend `tools/codex`/
  `tools/memory` depending on ownership preference.
- Query `messages` FTS or a new FTS table over visible final-sent content.
- Return snippets with timestamps, role, source, and message ids.
- Wrap output as untrusted, because user/assistant transcript text may contain
  web/email/attachment content.

Candidate tools:

```text
session_search(query: str, limit: int = 8, since: str | null = null)
session_recent(limit: int = 20, source: str | null = null)
```

Hermes has `session_search`; Hikari can implement the small version locally.

### P2 - Google Tasks Mirror

Goal: connect open loops/reminders to Google Tasks without making Google Tasks
the source of truth on day one.

Implementation shape:

- Direct REST or narrow `gws` wrapper.
- Local mapping table:

```sql
CREATE TABLE IF NOT EXISTS google_task_links (
  local_kind TEXT NOT NULL,       -- reminder/task
  local_id TEXT NOT NULL,
  tasklist_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  remote_updated TEXT,
  last_sync_at TEXT NOT NULL,
  PRIMARY KEY (local_kind, local_id)
);
```

Candidate tools:

```text
google_tasks_list_lists()
google_tasks_list(tasklist_id: str, show_completed: bool = false)
google_tasks_create(tasklist_id: str, title: str, notes: str | null = null, due: str | null = null)
google_tasks_complete(tasklist_id: str, task_id: str)
```

Policy:

- create/complete: `defer` initially.
- delete/clear: `gatekeeper`.
- reads wrapped.

### P2 - Tool Audit Surface

Goal: make side effects visible.

Current Hikari already writes audit rows for wrapped outputs and approval/gating
paths. Build a small user-facing read layer:

```text
tool_audit_recent(limit: int = 20, filter: str | null = null)
approval_recent(limit: int = 10)
```

Telegram command options:

- `/tools` - last 10 tool calls and failures.
- `/approvals` - pending/recent approvals.
- `/audit` - short recent side effects.

This borrows Hermes/OpenClaw observability without building a dashboard.

### P3 - Curated `gws_helper`

Goal: get the useful `gws` helpers without exposing the whole CLI.

Candidate tool:

```text
gws_helper(action: enum, args: object, dry_run: bool = true)
```

Allowed actions initially:

- `calendar_agenda`
- `gmail_triage`
- `gmail_read`
- `drive_upload`
- `sheets_read`
- `sheets_append`
- `workflow_meeting_prep`
- `workflow_weekly_digest`
- `workflow_email_to_task`

Hard rules:

- Default `dry_run=true` for write-like helpers.
- No arbitrary command string.
- No passthrough service/method args.
- Full command preview in approval summary.
- Enforce timeout and max output bytes.
- Treat all output as untrusted.

This can live behind dispatch first, then graduate to Hikari's main utility
server after tests.

### P4 - Event Ingestion

Goal: proactive "important thing happened" without polling everything.

Do after P0-P3.

Start with Gmail watch only if inbox proactivity is a priority. Otherwise Drive
or Meet events may be more useful:

- Drive file/comment events for active project folders.
- Meet transcript/recording/smart-notes events if the user starts using Meet.
- Gmail INBOX label watch for a narrow query/profile.

Required pieces:

- Pub/Sub credentials and topic/subscription setup.
- Renewal job.
- dedupe/cursor table.
- event-interest scorer.
- quiet-hours and cadence integration.
- explicit user opt-in per event source.

## What To Avoid

Avoid:

- adding a full third-party Workspace MCP before fixing the current gates;
- exposing `gws` generated commands directly;
- adding Google Admin, Classroom, Vault, or Apps Script to the companion path;
- making Chat/Slack/Discord another inbound surface without a clear reason;
- turning briefs into a default news firehose;
- using Model Armor as a replacement for local untrusted-output wrapping.

## Concrete Next PR

If the next PR is one surgical move, make it this:

1. Rename the current Google policy tests into a broader write-policy suite.
2. Add an explicit allow/gate list for all Google Workspace writes documented in
   `agents/subagents/prompts/drive_gmail.prompt.md`.
3. Update `config/scopes.yaml` for those tools.
4. Update the drive_gmail prompt to match the real policy.
5. Add a short `codex/google-tool-policy.md` or `docs/google-tools.md` table so
   future tool additions do not silently fall through the wildcard.

That gets the ground under Hikari's feet before we bolt on Tasks, Contacts, or
`gws`. Fine. Boring first. This is one of the places boring is the sharp tool.

## Source Notes

Local files read:

- `codex/tool-surface-google-hermes-openclaw-2026-05-23.md`
- `config/tools.yaml`
- `config/scopes.yaml`
- `agents/subagents/prompts/drive_gmail.prompt.md`
- `tests/test_google_workspace_send_policy.py`
- `tests/test_tools_yaml.py`
- `tests/test_approval_matrix.py`
- `tests/test_external_wrap.py`
- `tools/approvals.py`
- `tools/gatekeeper_can_use_tool.py`

Primary/external sources:

- Google Workspace CLI: https://github.com/googleworkspace/cli
- Google Workspace CLI changelog: https://github.com/googleworkspace/cli/blob/main/CHANGELOG.md
- Google Workspace CLI skills index: https://github.com/googleworkspace/cli/blob/main/docs/skills.md
- Google Workspace Developer Tools MCP docs: https://developers.google.com/workspace/guides/developer-tools
- Google Tasks API: https://developers.google.com/workspace/tasks/reference/rest
- Google Tasks resource: https://developers.google.com/workspace/tasks/reference/rest/v1/tasks
- Google People connections: https://developers.google.com/people/api/rest/v1/people.connections/list
- Gmail push notifications: https://developers.google.com/workspace/gmail/api/guides/push
- Gmail users.watch: https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.watch
- Google Workspace Events: https://developers.google.com/workspace/events
- Google Workspace subscriptions: https://developers.google.com/workspace/events/reference/rest/v1/subscriptions
- Hermes tools: https://hermes-agent.nousresearch.com/docs/user-guide/features/tools/
- Hermes memory: https://hermes-agent.nousresearch.com/docs/user-guide/features/memory/
- Hermes sessions: https://hermes-agent.nousresearch.com/docs/user-guide/sessions/
- Hermes cron: https://hermes-agent.nousresearch.com/docs/user-guide/features/cron/
- Hermes MCP: https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp
- Hermes Google Workspace docs: https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/skills/google-workspace.md
- Hermes `gws` migration issue: https://github.com/NousResearch/hermes-agent/issues/411
- OpenClaw page: https://claw.biswas.me/
- OpenClaw coding-agent skill: https://github.com/openclaw/openclaw/blob/main/skills/coding-agent/SKILL.md
