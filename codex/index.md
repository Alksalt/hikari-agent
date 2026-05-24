---
title: Codex Reports Index
date: 2026-05-24
tags:
  - codex
  - reports
  - planning
  - second-pass
---

# Codex Reports Index

This directory contains independent second-pass review reports for Hikari
Agent. Use this index to pick a small related set of files for planning instead
of loading every report at once.

## Current Status

- Active second-pass reports: 10.
- Prior 2026-05-23 reports were intentionally removed before this run.
- Missing expected domains: Non-Google integrations and sprint coverage.
- Treat each report as an independent current-working-tree review, not as a
  proof that older findings were right or wrong.

## Planning Slices

### Runtime / Reliability

Use when planning live Claude SDK session ownership, user/proactive/internal
control split, final-sent persistence, send/retry behavior, media outbox
semantics, and message continuity after file/document turns.

- `second-pass-runtime-reliability-message-persistence-2026-05-24.md`
- `second-pass-media-pipeline-2026-05-24.md`
- `second-pass-conversation-evals-2026-05-24.md`

Tags: `#runtime` `#reliability` `#session` `#proactive` `#persistence`
`#media` `#conversation-quality`

### Tool Trust / Security

Use when planning gatekeeper previews, approval UX, registry fail-closed
behavior, external MCP auth, Apple Events policy, Google Workspace write gates,
and prompt/config drift.

- `second-pass-tool-security-2026-05-24.md`
- `second-pass-google-workspace-tool-surface-2026-05-24.md`
- `second-pass-ops-production-2026-05-24.md`

Tags: `#security` `#tools` `#mcp` `#permissions` `#approvals`
`#google-workspace` `#oauth`

### Telegram Operator Surface

Use when planning what the owner can inspect or control directly from Telegram:
status, tools, audit, approvals, memory, proactive behavior, reminders, daily
check-ins, command menus, inline buttons, and cockpit accuracy.

- `second-pass-telegram-ux-2026-05-24.md`
- `second-pass-product-capabilities-2026-05-24.md`
- `second-pass-google-workspace-tool-surface-2026-05-24.md`
- `second-pass-ops-production-2026-05-24.md`

Tags: `#telegram` `#ux` `#operator-control` `#observability`
`#approvals` `#reminders`

### Memory / Personalization

Use when planning Graphiti-vs-SQLite authority, fact correction/forgetting,
open-loop tasks, session search, provenance, reflection, media memory, and
recall trust.

- `second-pass-memory-graph-2026-05-24.md`
- `second-pass-conversation-evals-2026-05-24.md`
- `second-pass-media-pipeline-2026-05-24.md`
- `second-pass-product-capabilities-2026-05-24.md`

Tags: `#memory` `#graphiti` `#recall` `#personalization`
`#provenance` `#evals`

### Conversation / Media Quality

Use when planning generated conversation evals, voice drift scoring, refusal
recovery, belief-frame handling, photo/voice/document handling, generated
photos, stickers, EXIF/privacy controls, and media auditability.

- `second-pass-conversation-evals-2026-05-24.md`
- `second-pass-media-pipeline-2026-05-24.md`
- `second-pass-runtime-reliability-message-persistence-2026-05-24.md`

Tags: `#evals` `#conversation-quality` `#persona` `#media`
`#voice` `#photos` `#attachments`

### Ops / Immortality

Use when planning launchd supervision, backup/restore, dead-man checks,
startup health, `/status` parity, Cloudflare/external MCP operations, secrets,
credential rotation, and disaster recovery drills.

- `second-pass-ops-production-2026-05-24.md`
- `second-pass-cleanup-ci-packaging-2026-05-24.md`
- `second-pass-tool-security-2026-05-24.md`

Tags: `#ops` `#production` `#backup` `#restore` `#launchd`
`#cloudflare` `#health-checks`

### Product Workflow

Use when planning user-facing leverage: link shelf, Readwise/Reader, briefings,
shifts, wiki filing, reminders, daily check-ins, day receipts, coding workflow,
status/audit, and proactive source ownership.

- `second-pass-product-capabilities-2026-05-24.md`
- `second-pass-telegram-ux-2026-05-24.md`
- `second-pass-memory-graph-2026-05-24.md`
- `second-pass-google-workspace-tool-surface-2026-05-24.md`

Tags: `#product` `#workflow` `#briefings` `#shifts` `#readwise`
`#wiki` `#link-shelf`

### Cleanup / CI / Packaging

Use when planning test debt, stale code deletion, package shape, CI validity,
MCP server validation, eval runner freshness, duplicated skills, and whether
this repo should be a local app or a real installable package.

- `second-pass-cleanup-ci-packaging-2026-05-24.md`
- `second-pass-conversation-evals-2026-05-24.md`
- `second-pass-ops-production-2026-05-24.md`

Tags: `#cleanup` `#tests` `#ci` `#packaging` `#dead-code`

## File Catalog

### `second-pass-runtime-reliability-message-persistence-2026-05-24.md`

Independent review of runtime entrypoints, session lifecycle, proactive
persistence, `send_and_persist`, media outbox semantics, and visible Telegram
reply persistence.

Use for: runtime invariants, live session ownership, document-turn continuity,
durable outbound text, and direct Telegram send bypasses.

Tags: `#runtime` `#reliability` `#session` `#proactive` `#persistence`

### `second-pass-tool-security-2026-05-24.md`

Independent review of tool registry policy, gatekeeper approval previews,
Google/Notion/GitHub write gates, Apple Events, external OAuth, Playwright
policy, and cockpit auth-precheck drift.

Use for: consent boundaries, approval preview truthfulness, MCP registry
hardening, and prompt/config policy drift.

Tags: `#security` `#tools` `#mcp` `#permissions` `#approvals`

### `second-pass-google-workspace-tool-surface-2026-05-24.md`

Independent review of Google Workspace MCP pinning, read/write registry
coverage, auth precheck, scope probing, daily check-in adapters, and Google
prompt drift.

Use for: Gmail/Calendar/Drive safety, Google OAuth health, daily check-in
reliability, and Google tool expansion planning.

Tags: `#google-workspace` `#tools` `#oauth` `#calendar` `#gmail`

### `second-pass-telegram-ux-2026-05-24.md`

Independent review of Telegram command UX and operator cockpit coverage:
`/status`, `/tools`, `/audit`, `/settings`, `/memory`, `/approvals`,
`/proactive`, reminders, daily check-ins, and missing callback/button flows.

Use for: Telegram-native control, command discoverability, approval UX,
settings accuracy, and owner trust without SSH.

Tags: `#telegram` `#ux` `#operator-control` `#observability`

### `second-pass-conversation-evals-2026-05-24.md`

Independent review of conversation quality and eval coverage: runtime split,
belief framing, generated Hikari conversations, refusal persistence, Layer C
rubrics, golden chats, sycophancy/voice checks, and stale drift references.

Use for: eval roadmap, final-text persistence in edge paths, prompt leakage
tests, and quality gates for warmth, honesty, memory grounding, and tool
transparency.

Tags: `#evals` `#conversation-quality` `#persona` `#memory`

### `second-pass-media-pipeline-2026-05-24.md`

Independent review of voice/photo/document/media handling, generated photos,
stickers, untrusted file ingest, MIME trust, EXIF privacy, media outbox, and
media memory/auditability.

Use for: voice/photo/media pipeline planning, document follow-up continuity,
privacy controls, and generated-photo provider drift.

Tags: `#media` `#voice` `#photos` `#attachments` `#privacy`

### `second-pass-memory-graph-2026-05-24.md`

Independent review of SQLite memory, Graphiti outbox, recall precedence,
fact invalidation, task injection, provenance, session search, and `/memory`
surfaces.

Use for: Graphiti authority decisions, correction/forget workflows, open-loop
task sanitization, and memory provenance.

Tags: `#memory` `#graphiti` `#recall` `#personalization`

### `second-pass-ops-production-2026-05-24.md`

Independent review of production durability: launchd services, encrypted
backup, restore, dead-man monitor, startup health, `/status`, external MCP,
Cloudflare, Keychain, credential rotation, and polling tradeoffs.

Use for: making the system recoverable, restartable, observable, and boring to
operate for months.

Tags: `#ops` `#production` `#backup` `#restore` `#launchd`

### `second-pass-cleanup-ci-packaging-2026-05-24.md`

Independent review of tests, Ruff, CI, MCP validation, wheel packaging,
duplicated skills, stale modules, dead schema, copied test snippets, and
determinism issues.

Use for: cleanup sprints, CI trust, packaging decisions, and deleting stale
features without weakening coverage.

Tags: `#cleanup` `#tests` `#ci` `#packaging` `#dead-code`

### `second-pass-product-capabilities-2026-05-24.md`

Independent review of user-facing workflow leverage: cockpit/status, memory
review, link shelf, reminders, day receipts, daily check-ins, morning brief,
coding workflow, Readwise/Reader, wiki filing, shifts, and proactive source
ownership.

Use for: product roadmap, workflow priorities, proactive source dedupe,
Readwise/Reader MVP planning, and Telegram actions/buttons.

Tags: `#product` `#workflow` `#readwise` `#briefings` `#shifts`

## Still Uncovered

These expected review domains do not have second-pass files yet:

- Non-Google integrations:
  `second-pass-non-google-integrations-2026-05-24.md`
- Sprint coverage synthesis:
  `second-pass-sprint-coverage-2026-05-24.md`

After those land, update this index with catalog entries and add them to the
planning slices above.
