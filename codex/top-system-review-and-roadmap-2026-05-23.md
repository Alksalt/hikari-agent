# Hikari Top-System Review and Roadmap

Date: 2026-05-23
Scope: local repo review, subagent deep dives, Hermes Agent comparison, verification run, and roadmap for making Hikari a top-tier personal agent system.

## Executive Summary

Hikari is already a serious system. The core differentiator is not "agent can use tools"; many systems can do that now. The differentiator is the combination of:

- Telegram-native companion UX.
- Persistent local memory with facts, tasks, episodes, observations, peer model, and core blocks.
- A real runtime split between user turns, visible proactive turns, and internal control calls.
- Tool access that reaches the user's actual life: calendar, reminders, wiki, Google Workspace, GitHub, Apple services, receipts, links, photos, research, and local utilities.
- A character layer with continuity, taste, and relational cadence instead of generic productivity chat.

The biggest risks are not lack of capability. They are reliability, trust boundaries, operational legibility, and product surfacing. Hikari has deep machinery, but some of it is hidden, partially wired, or too dependent on convention.

Compared with Hermes Agent, Hikari is more companion-shaped and locally personal. Hermes is stronger at packaging: obvious docs, clear user-facing concepts, visible memory/security/integration surfaces, and a platform-like product story. The right move is to borrow Hermes's taxonomy and operator clarity without flattening Hikari into a generic agent workbench.

## Sources

Hermes references:

- Hermes Agent docs: https://hermes-agent.nousresearch.com/docs
- Hermes Agent GitHub: https://github.com/nousresearch/hermes-agent
- Messaging docs: https://hermes-agent.nousresearch.com/docs/user-guide/messaging
- Memory docs: https://hermes-agent.nousresearch.com/docs/user-guide/features/memory
- Security docs: https://hermes-agent.nousresearch.com/docs/user-guide/security

Local review sources:

- `codex/deep-architecture-review-2026-05-23.md`
- `codex/architecture-review-2026-05-23.md`
- `codex/security-solo-dev-deep-dive-2026-05-23.md`
- `codex/dead-code-dead-tests-deep-dive-2026-05-23.md`
- `codex/tool-surface-google-hermes-openclaw-2026-05-23.md`
- `codex/prompt_persona_deep_dive.md`
- Direct code review of runtime, Telegram bridge, proactive scheduler, hooks, tool policy, memory, and tests.

## Verification Snapshot

Commands run:

- `uv run python -m pytest -q`
  - Result: `1172 passed, 5 failed, 3 warnings`.
  - Failures: all in `tests/test_voice.py::test_live_voice[...]`.
  - Root issue: live voice tests are gated only by `CLAUDE_CODE_OAUTH_TOKEN`, not separated from default offline test runs.

- `uv run python scripts/validate_tool_registry.py`
  - Result: passed, `validate_tool_registry: clean.`

- `uv run ruff check .`
  - Result: failed with `273 errors`, `170 fixable`.
  - Meaning: the repo advertises Ruff, but the lint contract is not currently true.

Repository state at review time included untracked/new report files in `codex/` and active changes around Graphiti/Kuzu memory integration. This review does not assume those changes are merged or stable.

## What Hikari Already Has

### Runtime Architecture

The three-entrypoint split is the right foundation:

- `run_user_turn(user_text)` for real user messages.
- `run_visible_proactive(seed_prompt)` for visible proactive messages.
- `run_internal_control(prompt)` for stateless control prompts.

This is a major advantage over many agent systems. It protects the live conversation from internal bookkeeping, approval resumes, reminder composition, calendar fetches, and scoring prompts.

### Memory

The memory layer has strong bones:

- Bi-temporal facts.
- Status and invalidation.
- Fact supersession.
- Tasks and decay.
- Episodes/messages.
- Observations and noticings.
- Peer model.
- Core blocks.
- FTS/vector hooks.
- Recall tool boundary.

This is more ambitious than most agent memory implementations. The gap is not schema ambition; it is provenance, calibration, sanitization, inspectability, and avoiding split-brain memory when Graphiti is introduced.

### Tools

The tool surface is broad and unusually personal:

- Wiki.
- Google Workspace.
- GitHub.
- Apple notes/reminders/calendar.
- Receipts.
- Link shelf.
- Weather, places, arXiv, translation, currency, YouTube Music.
- Attachment reader.
- Python/calculation utilities.
- Photo generation.
- External MCP gateway.

The central `config/tools.yaml` registry is the right direction. The main issue is that some wildcard policy patterns are too permissive for upstream MCP drift.

### Companion Product

Telegram UX is already richer than a normal bot:

- Voice, photo, document, location, text, sticker, and reaction handling.
- `/start`, `/silence`, `/unsilence`, `/tasks`, `/cancel`, `/cost`, `/memory_diff`.
- Proactive heartbeat, reengagement, morning brief, calendar heartbeat, daily check-in, wiki ping.

The missing piece is a user-facing control surface: `/help`, `/status`, memory inspection/correction, and capability explanation.

## Top Risks

### P0/P1: Session Fork After Multimodal Turns

`run_user_turn_blocks()` can update the stored `session_id` through an ephemeral SDK path while the persistent live client remains attached to the previous session. The next normal text or proactive turn can continue from the stale live session and overwrite state with a branch that never saw the document/image turn.

Needed:

- Invalidate or reconnect the persistent live client after any successful block/multimodal turn.
- Add a regression test that simulates a stale live client after `run_user_turn_blocks()`.

### P0/P1: Visible Message Persistence Is Not Single-Owned

Some paths send visible text and persist afterward. Some call a helper that only sends. This creates risk that a message is delivered to the user but absent from `messages`, handoff, memory, reengagement logic, and future context.

Needed:

- One explicit send-and-persist API for all visible assistant/proactive messages.
- Tests for heartbeat, reengagement, calendar heartbeat, daily check-in, wiki ping, and manual bridge sends.

### P1: Proactive Jobs Can Collide

Independent APScheduler jobs can pass gates in the same tick, serialize on the runtime lock, then both send. `last_proactive_sent` is stamped after success, too late to prevent concurrent eligibility.

Needed:

- Global proactive reservation.
- Final gate immediately before send.
- Reservation release on failure.
- Tests proving two jobs cannot both send from the same silence window.

### P1: Tool Trust Boundary Is Too Soft In Places

`python_run` is defer-gated but its macOS sandbox starts from allow-default, so approved snippets can still read broad local files. Wildcard MCP policy can also auto-allow newly introduced upstream write/destructive tools.

Needed:

- Deny-by-default read sandbox for `python_run`.
- Explicit input-file allowlist.
- Fail-closed MCP schema drift validator.
- Split read/write tool policy instead of broad wildcard allow.

### P1: Always-On Memory Can Carry Prompt-Shaped Text

`update_core_block`, peer model, observations, and noticings can persist model-generated text that later becomes always-on prompt context. Some paths sanitize; others do not.

Needed:

- Shared sanitizer for all memory text injected into prompts.
- Allowlist for core block labels.
- Reject instruction-shaped memory content.
- Render all ambient memory as quoted data, not free-standing instruction-like headings.

### P1: Graphiti/Kuzu Is Not Yet A Memory Layer

Graphiti is being dual-written, but normal recall still uses SQLite. Writes are fire-and-forget. There is no durable outbox, reconciliation, backfill, or recall integration. It also requires `ANTHROPIC_API_KEY`, which may conflict with the Max/Claude SDK billing story.

Needed:

- Decide whether Graphiti is experimental, read-side, or future primary memory.
- If keeping it: durable outbox, retry, backfill, reconciliation, and `/memory_diff` as an operator tool.
- If not ready: feature flag it off by default and prevent it from becoming a second source of truth.

### P1: Tests And CI Do Not Match The Promise

Default pytest is not clean in an environment with live auth. Ruff is red. CI appears absent. README claims are stale.

Needed:

- Mark live tests separately.
- Make default tests deterministic/offline.
- Add CI for pytest, Ruff, registry validation, `.mcp.json --check`, and compile checks.
- Refresh README as an operator runbook.

## Hermes Comparison

Hermes Agent appears strongest as a general personal-agent platform:

- Clear docs.
- Clear feature taxonomy.
- Memory, messaging, security, settings, integrations, and files are visible product concepts.
- MCP/tool extensibility is part of the story.
- User onboarding is easier because the system explains itself.

Hikari's advantages:

- More emotionally coherent companion layer.
- Stronger local-life integration.
- Better runtime separation for internal control calls.
- Telegram-native rituals and reengagement.
- Richer private memory primitives.

What to copy:

- A visible feature taxonomy.
- A user-facing trust/security page or command.
- Memory controls.
- Integration docs.
- A clear operator setup/status/runbook.

What not to copy:

- Generic agent blandness.
- Tool-sprawl as identity.
- Memory that is marketed but not inspectable.
- Platform docs that obscure the actual relationship model.

## What Else Is Needed To Become A Top System

### 1. Reliability Spine

Top systems do not merely generate good replies. They survive weird timing, restarts, partial sends, failed DB writes, stale sessions, scheduler overlap, OAuth expiry, and dependency drift.

Needed capabilities:

- Durable outbound message ledger.
- Idempotency keys for visible sends.
- Global proactive reservation.
- Durable background job state.
- Migration ledger.
- Backup/restore drill.
- Startup health checks.
- `/status` command.

### 2. Trust And Permission Model

The agent needs a user-visible trust model and an internal capability model.

Needed capabilities:

- Tool policy as typed capability groups: read, write, destructive, network, local-file, external-account.
- Exact allowlists for high-risk MCPs.
- Approval previews that are truthful and non-truncated for critical args.
- Deny-by-default local file sandboxing.
- Secrets and DB permission checks at startup.
- OAuth/DCR/token rate limits.
- Audit trail for tool calls that affect external systems.

### 3. Memory You Can Inspect And Correct

Memory should become a product, not just a retrieval backend.

Needed capabilities:

- "What do you remember about X?"
- "Why did you bring that up?"
- "Forget this."
- "Correct this."
- "Show open loops."
- Fact provenance: source message id, source span/hash, timestamp, attribution.
- Canonical entities for people, projects, places, apps, and aliases.
- Recall confidence calibrated against a memory eval set.

### 4. Evaluation Harness

A top companion needs evals for more than pass/fail unit behavior.

Needed eval suites:

- Voice/persona regression.
- Memory recall and contradiction handling.
- Proactive cadence and non-annoyance.
- Tool safety and approval bypass attempts.
- Prompt-injection resistance from wiki/web/email/calendar.
- Runtime session continuity.
- Long-running conversation replay.

### 5. Product Legibility

The system should be understandable from inside Telegram.

Needed commands:

- `/help`: capabilities and examples.
- `/status`: process, scheduler, auth, DB, backups, silence mode, last proactive send, pending approvals.
- `/memory`: inspect/correct/forget facts and open loops.
- `/tools`: connected integrations and permission state.
- `/settings`: proactive cadence, morning brief, reminders, memory sensitivity.

### 6. Operator Runbook

README should become the daily-use truth.

Needed sections:

- Install.
- Configure secrets.
- Start/stop launchd.
- Logs.
- Health checks.
- Backups and restore.
- OAuth refresh.
- Test tiers.
- Lint/format.
- Tool registry workflow.
- How to recover from stale SDK session, DB lock, scheduler failure, and MCP auth failure.

### 7. Typed Workflows Instead Of Prompt-Mediated Plumbing

Some scheduled workflows use internal prompts plus YAML parsing for things that should be typed adapters. Natural language is good for composition and judgment; it is weak for operational plumbing.

Needed migrations:

- Calendar fetch to typed adapter.
- Reminder sync to typed adapter.
- Approval defer/resume to one approval service.
- Proactive candidate scoring can stay model-assisted, but final send/persist/cadence should be typed.

### 8. Capability Packaging

Hikari needs a concise product map:

- Memory.
- Tools.
- Integrations.
- Automations.
- Files.
- Security.
- Settings.
- Rituals.

This is the Hermes lesson. The user should be able to see what exists without reading source code.

## Recommended Execution Waves

### Wave 1: Foundation First

Goal: make the current system reliable enough to build on.

Tasks:

1. Reconnect live SDK client after block/multimodal turns.
2. Centralize visible send-and-persist semantics.
3. Add proactive reservation/final gate.
4. Mark live voice tests as live/slow and make default pytest offline.
5. Add CI skeleton.
6. Lock down `python_run` local file reads.
7. Replace broad high-risk MCP wildcard allow with fail-closed checks.
8. Sanitize always-on memory injection.

Exit criteria:

- Default pytest is green and offline.
- Registry validation is green.
- Ruff is either green or intentionally narrowed with documented debt.
- No visible proactive message can be sent without a persisted message row or durable degraded record.
- No two proactive jobs can send from one eligibility window.

### Wave 2: Memory Product

Goal: make memory trustworthy and inspectable.

Tasks:

1. Add fact provenance.
2. Add memory inspection commands.
3. Add correction/forget UX.
4. Add canonical entities and aliases.
5. Add memory eval set.
6. Decide Graphiti status: integrate properly or disable by default.

Exit criteria:

- User can inspect, correct, and delete memory from Telegram.
- Recall confidence is tested against representative queries.
- Graphiti cannot silently diverge from SQLite.

### Wave 3: Operator And Product Legibility

Goal: make the system understandable and operable.

Tasks:

1. Add `/help`.
2. Add `/status`.
3. Add `/tools`.
4. Refresh README into runbook.
5. Add docs for trust model and tool permissions.
6. Add startup health report.

Exit criteria:

- A new operator can start, monitor, test, and recover Hikari without reading source.
- The user can see what Hikari can do from Telegram.

### Wave 4: Durable Agent Platform

Goal: make Hikari resilient under long-lived daily use.

Tasks:

1. Durable job ledger.
2. Background resume.
3. Message outbox and idempotency keys.
4. Migration ledger.
5. Backup restore tests.
6. External MCP package pinning and update workflow.
7. Security rate limits and audit views.

Exit criteria:

- Process restarts do not lose in-flight visible work.
- Schema upgrades are versioned.
- External dependency changes cannot silently alter tool permissions.

### Wave 5: Top-System Evals

Goal: prove quality continuously.

Tasks:

1. Voice/persona replay suite.
2. Long conversation replay suite.
3. Memory recall benchmark.
4. Proactive annoyance/cadence tests.
5. Prompt-injection corpus for web/wiki/email/calendar.
6. Tool permission bypass corpus.

Exit criteria:

- Every meaningful architectural promise has a regression test.
- Hikari can improve without silently becoming generic, unsafe, or forgetful.

## Immediate Recommendation

Start with Wave 1. It is the least glamorous and the most important. Hikari already has enough features to feel powerful. The next leap comes from making her hard to break:

- no session forks,
- no phantom visible messages,
- no proactive pileups,
- no soft sandbox leaks,
- no wildcard tool surprises,
- no unsanitized always-on memory,
- no red default test suite.

After that, build the Hermes-style product surface: `/help`, `/status`, `/memory`, trust docs, and a current README. That will make the existing depth visible instead of hidden in the wiring.

