---
title: Codex Reports Index
date: 2026-05-23
tags:
  - codex
  - reports
  - planning
---

# Codex Reports Index

This directory contains review reports and planning memos for Hikari Agent. Use
this index to pick a small related set of files for the next planning pass
instead of loading every report at once.

## How To Use

- Start with `top-system-review-and-roadmap-2026-05-23.md` when planning across
  the whole system.
- Pick one planning slice below, then read only the listed files.
- Use the tags to find reports by concern: architecture, security, tools,
  product UX, ops, cleanup, runtime/persona, modernity, priority, or roadmap.
- When a deep dive extends an earlier report, prefer the deep dive for current
  planning and use the earlier report for context.

## Planning Slices

### Foundation / Reliability

Use when planning runtime invariants, message persistence, proactive behavior,
approval lifecycle, Graphiti/Kuzu durability, or system ownership boundaries.

- `top-system-review-and-roadmap-2026-05-23.md`
- `deep-architecture-review-2026-05-23.md`
- `architecture-review-2026-05-23.md`
- `prompt_persona_deep_dive.md`

Tags: `#roadmap` `#architecture` `#runtime` `#memory` `#approvals`
`#proactive` `#persona`

### Ops / Immortality

Use when planning how to keep Hikari alive, observable, restartable,
recoverable, backed up, and restorable on a new machine.

- `ops-production-runbook-2026-05-23.md`
- `top-system-review-and-roadmap-2026-05-23.md`
- `deep-architecture-review-2026-05-23.md`
- `security-solo-dev-deep-dive-2026-05-23.md`

Tags: `#ops` `#production` `#runbook` `#launchd` `#backup`
`#restore` `#health-checks` `#cloudflare`

### Tool Trust / Permissions

Use when planning registry hardening, Google Workspace write gates, subagent
tool scope, external MCP drift, local side effects, or approval previews.

- `tool-subagent-risk-deep-dive-2026-05-23.md`
- `tool-subagent-inventory-2026-05-23.md`
- `tool-surface-deep-dive-2026-05-23.md`
- `security-review-2026-05-23.md`

Tags: `#tools` `#subagents` `#security` `#registry` `#mcp`
`#google-workspace` `#approvals`

### Non-Google Integrations

Use when planning Notion, GitHub, Playwright, Apple Events, Apple Shortcuts,
DuckDB/MotherDuck, YouTube Transcript, YouTube Music, Linear, Apple Notes, Link
Shelf, OpenRouter photo generation, Whisper STT, translation, weather, places,
currency, or arXiv.

- `non-google-integrations-deep-review-2026-05-23.md`
- `other-tools-review-2026-05-23.md`
- `tool-subagent-risk-deep-dive-2026-05-23.md`
- `security-review-2026-05-23.md`

Tags: `#non-google` `#integrations` `#notion` `#github`
`#apple-events` `#apple-shortcuts` `#duckdb` `#youtube` `#linear`

### Solo-Dev Security

Use when choosing the smallest security changes that matter for a single-user
local Telegram agent: prompt injection, local secret reads, package pinning,
approval preview safety, backup/token handling, and local automation.

- `security-solo-dev-deep-dive-2026-05-23.md`
- `security-review-2026-05-23.md`
- `tool-subagent-risk-deep-dive-2026-05-23.md`

Tags: `#security` `#solo-dev` `#prompt-injection` `#secrets`
`#supply-chain` `#approvals`

### Google / Tool Expansion

Use when deciding what tools to add next and what to avoid. This slice is useful
for Google Tasks, Contacts/People, Workspace Events, `gws`, Hermes/OpenClaw
patterns, session search, and tool observability. Start with the correction
memo if the question is product priority rather than Google-specific safety.

- `tool-priority-correction-2026-05-23.md`
- `tool-surface-deep-dive-2026-05-23.md`
- `tool-surface-google-hermes-openclaw-2026-05-23.md`
- `tool-subagent-inventory-2026-05-23.md`

Tags: `#tools` `#google-workspace` `#gws` `#hermes` `#openclaw`
`#mcp` `#observability` `#priority`

### Product Tool Priorities

Use when deciding which tool family actually matters next for Hikari's core
companion loop. This slice is the counterweight to Google-heavy planning: it
prioritizes status/audit, memory/session search, Readwise/Reader, briefings,
shift logistics, coding workflow, link intake, and local OS policy.

- `tool-priority-correction-2026-05-23.md`
- `ux-review-what-user-wants-2026-05-23.md`
- `top-system-review-and-roadmap-2026-05-23.md`
- `tool-surface-deep-dive-2026-05-23.md`

Tags: `#priority` `#tools` `#product` `#memory` `#readwise`
`#briefings` `#shifts` `#coding-workflow` `#audit`

### Non-Google Product Capabilities

Use when planning Hikari-native capabilities rather than raw connectors:
status/audit visibility, session search, memory controls, Readwise/Reader,
link/wiki intake, briefings, shifts, project workflow, local OS policy, and
optional media/taste tools.

- `other-tools-review-2026-05-23.md`
- `tool-priority-correction-2026-05-23.md`
- `ux-review-what-user-wants-2026-05-23.md`
- `top-system-review-and-roadmap-2026-05-23.md`

Tags: `#product` `#tools` `#readwise` `#briefings` `#shifts`
`#project-workflow` `#link-shelf` `#wiki` `#memory`

### Product / User Control Surface

Use when planning Telegram commands, inline buttons, proactive controls, memory
ledger UX, integration health, approval history, and "why did you say this?"
surfaces.

- `telegram-ux-design-2026-05-23.md`
- `ux-review-what-user-wants-2026-05-23.md`
- `top-system-review-and-roadmap-2026-05-23.md`
- `tool-surface-deep-dive-2026-05-23.md`

Tags: `#ux` `#telegram` `#memory` `#proactive` `#product`
`#observability`

### Conversation / Media Quality

Use when planning Hikari's visible conversation quality: voice/persona evals,
golden chats, proactive usefulness, memory-grounded answers, tool transparency,
refusal recovery, voice notes, photos, documents, attachments, generated
photos, stickers, and media persistence.

- `conversation-quality-evals-review-2026-05-23.md`
- `voice-photo-media-pipeline-review-2026-05-23.md`
- `prompt_persona_deep_dive.md`
- `telegram-ux-design-2026-05-23.md`

Tags: `#evals` `#persona` `#conversation-quality` `#voice`
`#photos` `#media` `#attachments` `#telegram`

### Cleanup / Test Debt

Use when planning dead-code removal, stale schema cleanup, unused helpers,
compatibility shim deletion, stale tests, and a realistic check/CI contract.

- `dead-code-dead-tests-deep-dive-2026-05-23.md`
- `dead-code-dead-tests-review-2026-05-23.md`
- `2026-05-23-modernity-architecture-review.md`

Tags: `#cleanup` `#tests` `#dead-code` `#schema` `#ci` `#ruff`

### Modernity / Platform Choices

Use when deciding whether to keep custom orchestration, adopt durable runtime
patterns, update MCP authorization, fix packaging, or compare current design
against LangGraph, Pydantic AI, Google ADK, OpenAI Agents SDK, Letta, Graphiti,
Mem0, Hermes, or OpenClaw.

- `2026-05-23-modernity-architecture-review.md`
- `top-system-review-and-roadmap-2026-05-23.md`
- `tool-surface-google-hermes-openclaw-2026-05-23.md`
- `deep-architecture-review-2026-05-23.md`

Tags: `#modernity` `#architecture` `#mcp` `#packaging` `#durability`
`#frameworks`

## File Catalog

### `top-system-review-and-roadmap-2026-05-23.md`

Whole-system roadmap and prioritization memo. It synthesizes local reviews,
Hermes comparison, verification results, current strengths, top risks, and
five execution waves.

Use for: choosing sprint order, deciding what makes Hikari a "top system",
turning scattered findings into a sequenced roadmap.

Tags: `#roadmap` `#top-system` `#architecture` `#security` `#memory`
`#product` `#evals`

### `ops-production-runbook-2026-05-23.md`

Operational runbook review for making Hikari durable as a single-user local
macOS Telegram companion with optional Cloudflare-exposed external MCP. It
covers launchd supervision, restart/recovery, health checks, backups, restore
drills, new-machine rebuilds, Cloudflare/external MCP operations, Telegram
silence debugging, credential rotation, logging, alerting, and an immortality
ladder.

Use for: making Hikari restartable, observable, recoverable, and boring to run
for months.

Tags: `#ops` `#production` `#runbook` `#launchd` `#backup`
`#restore` `#health-checks` `#cloudflare` `#telegram`

### `deep-architecture-review-2026-05-23.md`

Deep invariant review focused on where system ownership is unclear: inbound
event ledger semantics, durable side effects, approval architecture, policy
registry, internal control prompts, Graph memory contract, capability health,
and SDK lifecycle locking.

Use for: making runtime behavior explicit before adding more memory,
proactive, approval, or tool-adapter features.

Tags: `#architecture` `#runtime` `#invariants` `#messages`
`#approvals` `#memory` `#proactive`

### `architecture-review-2026-05-23.md`

Earlier architecture review centered on ownership boundaries and side channels.
Key topics are Graphiti/Kuzu as a non-durable sidecar, split approval state
machines, utility auto-discovery bypassing policy registration, prompt-mediated
scheduled workflows, background dispatch resumption, and oversized modules.

Use for: context before the deep architecture review, especially Graphiti/Kuzu,
approval, and modularity planning.

Tags: `#architecture` `#graphiti` `#memory` `#approvals` `#scheduler`
`#modularity`

### `prompt_persona_deep_dive.md`

State-boundary review for what Telegram showed, what SQLite persisted, and what
the Claude SDK resumed session remembered. It identifies divergence between
visible messages, logged messages, and hidden control prompts.

Use for: planning final-message persistence, proactive/session boundaries,
reflection safety, handoff correctness, and persona continuity fixes.

Tags: `#runtime` `#persona` `#messages` `#sqlite` `#session`
`#reflection` `#proactive`

### `security-review-2026-05-23.md`

General security review covering tool approval previews, DuckDB, `python_run`,
floating MCP dependencies, attachment delimiter escaping, Apple automation,
OAuth token storage, backups, external MCP OAuth/DCR routes, and scope precheck.

Use for: broad security hardening, regression-test planning, and confirming
which controls already exist.

Tags: `#security` `#tools` `#oauth` `#secrets` `#attachments`
`#sandboxing` `#mcp`

### `security-solo-dev-deep-dive-2026-05-23.md`

Security review recalibrated for a solo developer running one local personal
agent. It frames realistic attack chains and separates "fix now", "fix soon",
and "accept for now" work.

Use for: picking the smallest high-leverage security sprint without importing
enterprise security machinery.

Tags: `#security` `#solo-dev` `#prompt-injection` `#secrets`
`#supply-chain` `#apple-events`

### `non-google-integrations-deep-review-2026-05-23.md`

Deep review of the non-Google integration surface: Notion, GitHub, Playwright,
Apple Events, Apple Shortcuts, DuckDB/MotherDuck, YouTube Transcript, YouTube
Music, Linear, Apple Notes, Link Shelf, OpenRouter photo generation, Whisper
STT, translation, weather, places, currency, and arXiv. It maps auth, exposed
tools, writes/destructive actions, gate policy, wrapping, coverage, and
cross-cutting risks.

Use for: tightening non-Google connector policy and deciding which integrations
deserve product work versus safety cleanup.

Tags: `#non-google` `#integrations` `#tools` `#notion` `#github`
`#apple-events` `#apple-shortcuts` `#duckdb` `#youtube` `#linear`

### `tool-subagent-inventory-2026-05-23.md`

Inventory of the tool and subagent surface: in-process MCP servers, utility
tools, external MCP servers, Google Workspace detail, scheduled/indirect flows,
operator scripts, external remote MCP, and observed test coverage.

Use for: understanding the current tool map before changing registry policy,
subagent grants, or MCP server configuration.

Tags: `#tools` `#subagents` `#inventory` `#registry` `#mcp`
`#google-workspace` `#tests`

### `tool-subagent-risk-deep-dive-2026-05-23.md`

Risk-focused follow-up to the inventory. It prioritizes wildcard external
grants, Google/Notion/GitHub write fallthrough, registry validation, local side
effects, hard deletes, Apple automation, package pinning, and observability.

Use for: planning the next permission-model PR and writing regression tests for
tool policy drift.

Tags: `#tools` `#subagents` `#security` `#registry` `#wildcards`
`#side-effects` `#approvals`

### `tool-surface-deep-dive-2026-05-23.md`

Current tool-roadmap memo. It argues for a narrower, better-specified tool
contract before expansion: explicit Google write gates, smaller Google helpers,
session/tool audit surfaces, event/routine tools after lifecycle work, and
curated `gws` use instead of raw exposure.

Use for: planning tool expansion, especially Google Workspace and tool-call
observability.

Tags: `#tools` `#google-workspace` `#gws` `#observability`
`#roadmap` `#mcp`

### `tool-surface-google-hermes-openclaw-2026-05-23.md`

Earlier tool-surface research comparing local Hikari tools with Google
Workspace CLI, Hermes Agent, and OpenClaw. It identifies useful external
patterns and candidate tools such as Tasks, People/Contacts, Workspace Events,
and Google helper workflows.

Use for: external inspiration and comparison before deciding what Hikari should
copy, adapt, or avoid.

Tags: `#tools` `#google-workspace` `#gws` `#hermes` `#openclaw`
`#research`

### `tool-priority-correction-2026-05-23.md`

Correction memo for the tool roadmap. It says the previous tool deep dives
were right to treat Google as a safety risk, but wrong to treat Google as the
highest-value product expansion. It ranks status/audit, memory/session search,
Readwise/Reader, brief sources, shift logistics, coding workflow, link shelf
hardening, Apple/local OS policy, then Google cleanup and selected Google
additions.

Use for: choosing the next product-oriented tool sprint and preventing the
roadmap from over-rotating on Google just because Google is the sharpest
existing risk surface.

Tags: `#priority` `#tools` `#product` `#memory` `#readwise`
`#briefings` `#shifts` `#coding-workflow` `#audit` `#google-workspace`

### `other-tools-review-2026-05-23.md`

Non-Google product/tool roadmap. It ranks visibility/trust, memory/session
search, Readwise/Reader, link/wiki intake, brief source registry, shifts,
health/fitness logs, GitHub/Linear/project workflow, background worker
observability, Apple local OS policy, Telegram UX, optional Slack/Discord,
creator workflow, DuckDB analytics, and finance.

Use for: choosing practical non-Google capabilities that make Hikari more
continuous and useful in daily life.

Tags: `#tools` `#product` `#non-google` `#readwise` `#briefings`
`#shifts` `#project-workflow` `#link-shelf` `#wiki` `#apple-shortcuts`

### `ux-review-what-user-wants-2026-05-23.md`

Product and UX review. It argues that backend capability is ahead of the user
surface, and recommends Telegram-native commands/buttons, proactive source
controls, memory ledger UX, trust/tool transparency, integration health, and a
small cockpit only if Telegram becomes too dense.

Use for: planning the next user-visible control surface and making existing
capabilities discoverable.

Tags: `#ux` `#telegram` `#product` `#memory` `#proactive`
`#approvals` `#integration-health`

### `telegram-ux-design-2026-05-23.md`

Detailed Telegram-first UX specification. It defines command surfaces,
owner-only menu commands, callback data contracts, inline button layouts,
approval flow, `/status`, `/tools`, `/audit`, `/memory`, `/proactive`,
daily check-in, reminders, settings, failure-message copy, implementation
waves, and test plans.

Use for: implementing Hikari's Telegram cockpit before a Mini App or web UI.

Tags: `#ux` `#telegram` `#commands` `#inline-buttons` `#approvals`
`#status` `#audit` `#memory` `#proactive`

### `conversation-quality-evals-review-2026-05-23.md`

Conversation-quality and eval-system review. It covers current persona and
voice tests, sycophancy resistance, proactive usefulness, memory-grounded
answers, tool transparency, refusal/recovery, drift detection, missing eval
dimensions, golden chat design, scoring rubrics, automation phases, and CI
versus manual evaluation.

Use for: building a measurable quality harness for Hikari's personality,
memory use, tool behavior, and long-run continuity.

Tags: `#evals` `#conversation-quality` `#persona` `#memory`
`#proactive` `#golden-chats` `#drift`

### `voice-photo-media-pipeline-review-2026-05-23.md`

Media pipeline review for Telegram voice notes, transcription, user photos,
generated photos, document/image attachments, sticker handling, persistence,
media safety, prompt injection, final-message persistence, and Graphiti/memory
behavior around media events.

Use for: hardening media ingestion and making media turns durable, safe, and
consistent with text-message persistence.

Tags: `#media` `#voice` `#photos` `#attachments` `#stickers`
`#transcription` `#prompt-injection` `#memory`

### `dead-code-dead-tests-review-2026-05-23.md`

Initial dead-code and stale-test review. It identifies fully dead Notion cache
code, removed SPASM/persona-drift probe leftovers, orphaned Day Receipt helpers,
small unused helpers, duplicated skill folders, stale live voice test gating,
and unused import/local cleanup.

Use for: first-pass cleanup planning and removing obvious stale surfaces.

Tags: `#cleanup` `#dead-code` `#tests` `#schema` `#notion`
`#day-receipt`

### `dead-code-dead-tests-deep-dive-2026-05-23.md`

Deeper cleanup pass covering compatibility shims kept alive by tests, dead
`voice_critic_log` schema, write-only consolidation summaries/relation edges,
orphan OAuth cleanup, test-only budget counters, test-only analytics readbacks,
Graphiti boot test quality, and unused import/local growth.

Use for: cleanup sequencing after the initial dead-code report, especially
where tests preserve old contracts.

Tags: `#cleanup` `#dead-code` `#tests` `#schema` `#compatibility`
`#graphiti`

### `2026-05-23-modernity-architecture-review.md`

Modernity review against current agent/tooling ecosystem. It flags MCP
authorization spec drift, non-green advertised quality gates, incomplete
packaging, custom orchestration vs durable runtimes, and private tool semantics
that should also use standard MCP annotations.

Use for: deciding whether to invest in packaging, CI/check cleanup, MCP spec
updates, or durable orchestration patterns.

Tags: `#modernity` `#architecture` `#mcp` `#oauth` `#ci`
`#packaging` `#frameworks`

## Tag Index

- `#approvals`: `prompt_persona_deep_dive.md`,
  `security-review-2026-05-23.md`,
  `security-solo-dev-deep-dive-2026-05-23.md`,
  `telegram-ux-design-2026-05-23.md`,
  `tool-subagent-risk-deep-dive-2026-05-23.md`,
  `ux-review-what-user-wants-2026-05-23.md`
- `#architecture`: `architecture-review-2026-05-23.md`,
  `deep-architecture-review-2026-05-23.md`,
  `2026-05-23-modernity-architecture-review.md`,
  `top-system-review-and-roadmap-2026-05-23.md`
- `#cleanup`: `dead-code-dead-tests-review-2026-05-23.md`,
  `dead-code-dead-tests-deep-dive-2026-05-23.md`
- `#conversation-quality`: `conversation-quality-evals-review-2026-05-23.md`
- `#evals`: `conversation-quality-evals-review-2026-05-23.md`,
  `top-system-review-and-roadmap-2026-05-23.md`
- `#google-workspace`: `tool-subagent-inventory-2026-05-23.md`,
  `tool-subagent-risk-deep-dive-2026-05-23.md`,
  `tool-priority-correction-2026-05-23.md`,
  `tool-surface-deep-dive-2026-05-23.md`,
  `tool-surface-google-hermes-openclaw-2026-05-23.md`
- `#memory`: `architecture-review-2026-05-23.md`,
  `conversation-quality-evals-review-2026-05-23.md`,
  `deep-architecture-review-2026-05-23.md`,
  `other-tools-review-2026-05-23.md`,
  `prompt_persona_deep_dive.md`,
  `telegram-ux-design-2026-05-23.md`,
  `tool-priority-correction-2026-05-23.md`,
  `top-system-review-and-roadmap-2026-05-23.md`,
  `ux-review-what-user-wants-2026-05-23.md`
- `#media`: `voice-photo-media-pipeline-review-2026-05-23.md`
- `#mcp`: `2026-05-23-modernity-architecture-review.md`,
  `non-google-integrations-deep-review-2026-05-23.md`,
  `ops-production-runbook-2026-05-23.md`,
  `security-review-2026-05-23.md`,
  `tool-subagent-inventory-2026-05-23.md`,
  `tool-subagent-risk-deep-dive-2026-05-23.md`,
  `tool-surface-deep-dive-2026-05-23.md`
- `#non-google`: `non-google-integrations-deep-review-2026-05-23.md`,
  `other-tools-review-2026-05-23.md`
- `#ops`: `ops-production-runbook-2026-05-23.md`
- `#photos`: `voice-photo-media-pipeline-review-2026-05-23.md`
- `#proactive`: `deep-architecture-review-2026-05-23.md`,
  `conversation-quality-evals-review-2026-05-23.md`,
  `prompt_persona_deep_dive.md`,
  `telegram-ux-design-2026-05-23.md`,
  `top-system-review-and-roadmap-2026-05-23.md`,
  `ux-review-what-user-wants-2026-05-23.md`
- `#security`: `security-review-2026-05-23.md`,
  `security-solo-dev-deep-dive-2026-05-23.md`,
  `tool-subagent-risk-deep-dive-2026-05-23.md`
- `#tests`: `2026-05-23-modernity-architecture-review.md`,
  `dead-code-dead-tests-review-2026-05-23.md`,
  `dead-code-dead-tests-deep-dive-2026-05-23.md`,
  `tool-subagent-inventory-2026-05-23.md`
- `#tools`: `tool-subagent-inventory-2026-05-23.md`,
  `non-google-integrations-deep-review-2026-05-23.md`,
  `other-tools-review-2026-05-23.md`,
  `tool-priority-correction-2026-05-23.md`,
  `tool-subagent-risk-deep-dive-2026-05-23.md`,
  `tool-surface-deep-dive-2026-05-23.md`,
  `tool-surface-google-hermes-openclaw-2026-05-23.md`
- `#telegram`: `ops-production-runbook-2026-05-23.md`,
  `telegram-ux-design-2026-05-23.md`,
  `ux-review-what-user-wants-2026-05-23.md`,
  `voice-photo-media-pipeline-review-2026-05-23.md`
- `#ux`: `telegram-ux-design-2026-05-23.md`,
  `ux-review-what-user-wants-2026-05-23.md`
- `#voice`: `conversation-quality-evals-review-2026-05-23.md`,
  `voice-photo-media-pipeline-review-2026-05-23.md`
- `#priority`: `tool-priority-correction-2026-05-23.md`,
  `other-tools-review-2026-05-23.md`,
  `top-system-review-and-roadmap-2026-05-23.md`
- `#readwise`: `other-tools-review-2026-05-23.md`,
  `tool-priority-correction-2026-05-23.md`
- `#briefings`: `other-tools-review-2026-05-23.md`,
  `tool-priority-correction-2026-05-23.md`
- `#shifts`: `other-tools-review-2026-05-23.md`,
  `tool-priority-correction-2026-05-23.md`
- `#audit`: `tool-priority-correction-2026-05-23.md`,
  `other-tools-review-2026-05-23.md`,
  `tool-surface-deep-dive-2026-05-23.md`
- `#project-workflow`: `other-tools-review-2026-05-23.md`,
  `tool-priority-correction-2026-05-23.md`
