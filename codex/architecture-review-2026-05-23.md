# Architecture Review - 2026-05-23

Scope: current working tree at `/Users/ol/agents/hikari-agent`.

Existing dirty files at review start: `agents/reflection.py`,
`agents/telegram_bridge.py`, `pyproject.toml`, `tools/memory/remember.py`,
`uv.lock`, plus untracked `storage/graph.py` and `tests/test_graph_phase_c.py`.
I treated those as user/current-branch work and did not revert anything.

This review intentionally does not repeat the separate modernity, security, or
dead-code reports already in `codex/`. It focuses on architectural ownership:
runtime boundaries, state consistency, policy surfaces, and where new features
are becoming side channels rather than first-class system components.

## Findings

### P1 - Graphiti/Kuzu memory is a non-durable sidecar, not an integrated memory layer

The new Graphiti/Kuzu layer is wired as best-effort dual-write from memory
insertion paths, but there is no durable outbox, no reconciliation job, no
backfill path, and no read-side integration with recall.

Evidence:

- `storage/graph.py:31-52` lazily creates a singleton Graphiti instance and
  requires `ANTHROPIC_API_KEY`.
- `storage/graph.py:63-90` wraps `add_episode` in `add_episode_safe()` and
  returns `False` on every failure after logging.
- `tools/memory/remember.py:46-55` writes the canonical SQLite fact and then
  schedules `add_episode_safe(...)` with `asyncio.create_task(...)`.
- `agents/reflection.py:131-143` and `agents/reflection.py:155-167` do the same
  for reflection-created facts and superseding facts.
- `agents/telegram_bridge.py:1934-1938` initializes the graph at boot, but graph
  failure is explicitly non-fatal.
- `tools/memory/recall.py:42` still reads only from `storage.retrieval.retrieve`;
  `storage/graph.py:93-100` exposes graph search, but it is not in the recall
  path.

Impact:

If the process exits after the SQLite write but before the background task runs,
or if Graphiti/Kuzu/API auth is unavailable, the graph permanently misses that
memory. The system then contains two memory stores with no declared source of
truth or convergence mechanism. Because recall ignores the graph today, the
sidecar is low blast-radius for answers, but high risk for future assumptions:
once graph search is used, it may be stale in ways the caller cannot detect.

Recommendation:

- Decide whether Graphiti is experimental telemetry or a second memory index.
- If experimental, name it that way and keep it out of user-visible recall.
- If it is meant to become real memory, add a durable `graph_outbox` table:
  `id`, `source_table`, `source_id`, `payload_json`, `status`, `attempts`,
  `last_error`, `created_at`, `processed_at`.
- Replace fire-and-forget dual writes with outbox inserts in the same SQLite
  transaction as the fact/episode write.
- Add a retry worker and a backfill/reconcile command that can rebuild Kuzu from
  SQLite.
- Only then add Graphiti as a recall candidate source, with result provenance.

### P1 - Approval is split across two state machines that share one table

There are two owner-approval architectures running at once: a `PreToolUse`
defer path and an SDK `can_use_tool` gatekeeper path. They share the
`approvals` table and the same Telegram reply resolver, but their lifecycle,
timeout, uniqueness, cancellation, and execution semantics differ.

Evidence:

- `agents/hooks.py:703-827` implements the `PreToolUse` defer state machine:
  create an approval row, schedule timeout, send a Telegram prompt, and return
  `permissionDecision="defer"`.
- `tools/gatekeeper_can_use_tool.py:69-127` implements the separate
  `can_use_tool` gatekeeper state machine.
- `tools/approvals.py:186-270` contains the shared reply resolver and branches
  between gatekeeper rows, SDK-defer rows, and legacy callback rows.
- `storage/db.py:957-973` creates partial uniqueness only for
  `gate_kind='gatekeeper'`; legacy/defer rows have different queue behavior.
- `storage/db.py:2576-2584` resolves "the oldest still-pending approval" for a
  chat without first separating approval kind.
- `agents/hooks.py:640-647` and `tools/gatekeeper_can_use_tool.py:59-66` each
  render their own truncated approval summary, which is exactly the kind of
  duplicated user-decision surface that already shows up in the security review.

Impact:

This is more than cosmetic duplication. Every approval semantics change now has
to be implemented twice or consciously excluded from one path. Timeout,
implicit-cancel, restart recovery, audit, and preview safety can drift. Because
both paths share the same inbound Telegram phrase (`CONFIRM-SEND`) and the same
table, subtle ordering bugs become likely as more tools move between `defer`
and `gatekeeper`.

Recommendation:

- Collapse approval into one `approval_service` with a single state machine and
  one row shape.
- Model execution as a strategy on the row, not as separate state machines:
  `resume_same_tool`, `call_confirmed_sibling`, `sdk_permission_allow`,
  `callback`.
- Give every approval row a `kind`, `tool_use_id`, full structured preview, and
  explicit terminal status.
- Make `approval_pending_for(chat_id)` choose by state invariants, not oldest
  row across mixed systems.
- After consolidation, `PreToolUse` and `can_use_tool` should be thin adapters
  into the same service.

### P1 - Utility tool auto-discovery bypasses policy registration by design

The runtime auto-allows every utility tool exported through `ALL_TOOLS`, while
`config/tools.yaml` only carries policy metadata for selected utility tools.
The validator explicitly exempts `mcp__hikari_utility__*` names from YAML
registration.

Evidence:

- `agents/runtime.py:167-174` appends all
  `discover_utility_tool_names()` results to the SDK allowlist.
- `tools/_registry.py:73-94` imports every non-skipped `tools/*` module/package
  with an `ALL_TOOLS` list and collects those tools.
- `tools/README.md:93-97` tells authors they do not need to add utility tools
  to `agents/runtime.py`; the runtime derives the allowlist automatically.
- `scripts/validate_tool_registry.py:75-84` ignores utility tools that are not
  covered by explicit YAML policy.
- A local coverage check found auto-allowed utility tools with no explicit YAML
  policy, including `reminder_create`, `reminder_cancel`, `link_save`,
  `link_delete`, `decision_log_capture`, `decision_log_resolve`, and day-receipt
  write/delete tools.

Impact:

The drop-a-folder ergonomics are good, but they turn tool exposure into an
import side effect. A new utility feature can become callable with default
`gate=None` and `untrusted_output=False` simply by exporting `ALL_TOOLS`. That
is especially risky because several utility tools are state-changing personal
memory/log/reminder tools rather than harmless reads.

Recommendation:

- Keep auto-discovery for implementation wiring, but make policy registration
  mandatory.
- Add a validator failure for every discovered utility tool missing a
  `config/tools.yaml` entry, unless it is covered by a deliberately broad
  utility wildcard that sets safe defaults.
- Add fields for `read_only`, `destructive`, `external_io`, and
  `untrusted_output` so the policy review is visible.
- Prefer default-deny for newly discovered write tools until YAML policy exists.

### P2 - Scheduled workflows are still prompt-mediated where typed adapters should own the contract

Several cron/internal workflows ask the lead model to call a tool or subagent,
then parse YAML out of the model response. That keeps everything inside the
Agent SDK path, but it makes deterministic infrastructure jobs depend on prompt
following and text parsing.

Evidence:

- `agents/proactive.py:419-433` asks an internal-control prompt to delegate to
  the Google Workspace calendar tool and return strict YAML.
- `agents/proactive.py:439-449` parses that YAML defensively.
- `agents/proactive.py:735-751` mirrors reminders to Apple Reminders by asking
  the model to call `mcp__apple_events__reminders_tasks` and emit YAML.
- `agents/proactive.py:804-818` mirrors reminders to Google Calendar through a
  similar model-mediated prompt.
- `tools/approvals.py:358-390` formats a synthetic prompt that asks the model
  to execute approved deferred tool args.

Impact:

These jobs are infrastructure, not conversational reasoning. They need
idempotency, typed inputs, typed errors, and predictable retries more than they
need Hikari voice. Prompt-mediated execution increases latency and token cost,
and it introduces a fragile "the model must call the right tool and speak YAML"
contract into calendar/reminder/approval plumbing.

Recommendation:

- Introduce typed service adapters for recurring infrastructure operations:
  calendar fetch, calendar create, Apple reminder create, approved tool replay.
- If the only available path is MCP, wrap the MCP call in a narrow Python
  adapter so callers receive a typed result object, not YAML text.
- Keep `run_internal_control` for tasks that genuinely require model judgment:
  composition, classification, summarization, and ambiguous routing.
- Make synthetic approval execution a typed replay where possible, especially
  for tools whose args are already persisted as JSON.

### P2 - Background dispatch persists enough metadata to notice interruption, but not enough to resume it

The dispatch subsystem has a useful task table and event listener, but the
actual worker is an in-process `asyncio.Task`. On restart, running/queued work is
marked failed even when a Claude SDK session id had been persisted.

Evidence:

- `tools/dispatch/_shared.py:212-222` creates a `background_tasks` row and then
  starts `_run_session(...)` with `asyncio.create_task(...)`.
- `tools/dispatch/_shared.py:149-152` stores the SDK `session_id` when a
  `ResultMessage` arrives.
- `agents/background_listener.py:84-99` drains an in-memory queue forever.
- `agents/background_listener.py:174-193` says it cannot resume a Python-side
  task after restart, marks the row failed, and asks the user to re-dispatch.

Impact:

This is acceptable for casual side work, but it is not a durable worker system.
The table looks like a job ledger and stores a `session_id`, which creates the
expectation of resumability, while the runtime contract says restart means fail.
As dispatch gets used for longer code tasks, restart/sleep becomes a normal
failure mode rather than an edge case.

Recommendation:

- Either rename the contract as non-durable dispatch and keep tasks short, or
  promote it to a real durable worker.
- For durable dispatch, persist queued/running state transitions with leases,
  move events out of the in-memory queue, and add a `resume_task(task_id)` path
  that re-enters `_run_session` with the stored `session_id`.
- Record periodic heartbeats/tool progress in SQLite so restart recovery can
  distinguish "still probably running" from "died mid-turn."

### P2 - Core ownership boundaries are concentrated in oversized modules

The architecture has good conceptual boundaries, but several physical modules
now own too many unrelated responsibilities. This is making future changes more
dangerous because unrelated features share import paths, test fixtures, and
migration surfaces.

Evidence:

- `storage/db.py` is 3412 lines and contains schema DDL, migrations, connection
  pooling, memory CRUD, reminders, approvals, OAuth, future letters, decisions,
  telemetry, and pruners.
- `agents/telegram_bridge.py` is 1966 lines and owns Telegram transport,
  politeness gates, photo/voice/document ingest, choreography, command routing,
  reaction turns, scheduler startup, Google health probing, graph boot, dispatch
  listener startup, and post-send persistence.
- `agents/reflection.py` is 1161 lines and owns daily reflection, peer model
  updates, graph dual-writes, lexicon/task decay, drift telemetry, morning
  dispatch, topic consolidation, duplicate detection, and weekly consolidation.
- `agents/scheduler.py:19-322` wires all background jobs in one function.

Impact:

The problem is not file length by itself. It is cross-domain coupling. A schema
change for OAuth and a memory graph change both touch `storage/db.py`; a startup
change for Graphiti and a Telegram message ingest change both touch
`telegram_bridge.py`; a reflection-memory change can affect drift, lexicon,
weekly consolidation, and morning dispatch. This increases the odds of
incidental regressions and makes parallel work harder.

Recommendation:

- Split storage by bounded context while keeping one shared `_conn()` module:
  `storage/memory.py`, `storage/messages.py`, `storage/approvals.py`,
  `storage/reminders.py`, `storage/oauth.py`, `storage/telemetry.py`.
- Split bridge handlers by input type:
  `agents/bridge/text.py`, `photo.py`, `voice.py`, `document.py`,
  `reactions.py`, `commands.py`, plus a small `telegram_bridge.py` assembly
  module.
- Split reflection into orchestrator plus steps:
  `daily.py`, `weekly.py`, `graph_sync.py`, `topic_consolidation.py`,
  `morning_dispatch.py`.
- Do this incrementally around active work; no big-bang move.

## Strong Architecture To Preserve

- The three runtime entrypoints are the right conceptual split:
  `run_user_turn`, `run_visible_proactive`, and `run_internal_control` in
  `agents/runtime.py:468-566`.
- Post-send persistence is the correct invariant: visible assistant text is
  filtered, sent, then appended to `messages` only after delivery succeeds
  (`agents/telegram_bridge.py:179-345`).
- The `_RUN_LOCK` around live session resume is a good guard against forking the
  Claude SDK session (`agents/runtime.py:83-92`, `agents/runtime.py:479-488`,
  `agents/runtime.py:524-533`).
- Registry-driven external MCP/tool policy is the right direction; the gap is
  making utility tools participate in it.
- The untrusted-output hook is a good generic boundary
  (`agents/external_wrap_hook.py:132-188`), and it should remain a central
  defense layer rather than scattered hand-wrapping.
- SQLite as the local source of truth is appropriate for a single-user,
  privacy-sensitive companion. The needed change is better domain boundaries,
  not replacing SQLite.

## Suggested Refactor Order

1. Make utility tool policy registration mandatory. This is small and prevents
   future accidental exposure.
2. Decide Graphiti's role. If it is real memory, add the durable outbox before
   using graph search in recall.
3. Collapse approval/gatekeeper into one approval service and one preview
   renderer.
4. Extract typed adapters for calendar/reminder/approved-tool infrastructure
   workflows.
5. Start splitting `storage/db.py` and `telegram_bridge.py` only after the above
   semantics are stable.

## Verification

No runtime code was changed. I inspected architecture and wrote this report.
The only command that attempted package execution initially hit the sandboxed
default uv cache; rerunning with `UV_CACHE_DIR=/private/tmp/hikari-uv-cache`
succeeded and confirmed the utility-policy coverage gap listed above.
