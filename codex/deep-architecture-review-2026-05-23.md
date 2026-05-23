# Deep Architecture Review - Invariants and Failure Modes - 2026-05-23

Scope: current working tree at `/Users/ol/agents/hikari-agent`.

This is a companion to `codex/architecture-review-2026-05-23.md`. The first
review covered ownership boundaries at a system level. This pass goes deeper on
the invariants that should be explicit if the project is going to keep adding
memory, proactive behavior, approval flows, and tool adapters without creating
more side channels.

Existing dirty files were treated as current-branch/user work and left alone.
No runtime code was changed by this review.

## Executive read

The architecture has a strong center: the three runtime entrypoints in
`agents/runtime.py`, post-send persistence, a real `_RUN_LOCK` around live SDK
turns, SQLite as the canonical state store, and a growing registry direction for
tools.

The deeper issue is that several newer behaviors are still owned by local
handlers instead of by architectural services:

- inbound events are recorded differently by text, photo, voice, document,
  command, and reaction paths;
- durable side effects are mixed with best-effort `asyncio.create_task(...)`
  work;
- approval semantics are split between legacy defer and SDK gatekeeper flows;
- tool policy is partly registry-driven and partly auto-discovered;
- internal control prompts are used both for model judgment and for ordinary
  API transport;
- tests often pin historical fixes rather than expressing the desired global
  invariant.

The result is not chaos. It is something subtler: the code usually works because
each path carefully fixed its own bug. But the system no longer has one obvious
place to ask "what counts as a user event?", "what is durable?", "what needs
owner approval?", or "which calls are allowed to mutate external state?"

## Findings

### P1 - Inbound conversation events have no single architectural owner

Text turns have a central wrapper, but every non-text path hand-rolls its own
ledger semantics: whether to write a row to `messages`, whether to update
`runtime_state.last_user_message`, whether to use live session context, whether
to insert an episode, and whether the prompt text should be persisted.

Evidence:

- `agents/runtime.py:569-579` defines `respond()`: append the user text, bump
  `last_user_message`, then call `run_user_turn()`.
- `agents/telegram_bridge.py:541-550` records a compact photo event row, bumps
  `last_user_message`, then calls `run_user_turn()` with a synthetic prompt.
- `agents/telegram_bridge.py:555-563` additionally inserts a photo episode.
- `agents/telegram_bridge.py:655-664` does the same event-row plus live-turn
  pattern for voice notes.
- `agents/telegram_bridge.py:670-676` additionally inserts a voice episode.
- `agents/telegram_bridge.py:1110-1123` records a document event row, bumps
  `last_user_message`, then calls `run_user_turn_blocks()`. There is no matching
  document episode insertion in this path.
- `agents/telegram_bridge.py:1145-1159` records `[/start]`, bumps
  `last_user_message`, then uses `run_internal_control()` rather than a live
  turn.
- `agents/telegram_bridge.py:1712-1727` records a reaction event row and calls
  `run_user_turn()` with a synthetic prompt, but does not bump
  `last_user_message`.
- `agents/hooks.py:468-484` uses `last_user_message` to build the memory-injected
  gap-awareness block.
- `agents/proactive.py:76-92` uses `last_user_message` to suppress heartbeat
  sends after recent user activity.

Impact:

The code has good local comments, but no global event contract. A reaction is a
user action for conversation context, yet it does not count as user activity for
heartbeat suppression. Documents are durable message events, but unlike photos
and voice notes they do not become callback episodes. `/start` mutates the
ledger and activity timestamp without touching the live session. Some of this
may be intentional. The problem is that intent lives in handler comments, not in
a typed policy.

Recommendation:

Create a small conversation-ingestion service, for example
`ConversationLedger.record_inbound_event(...)`, that owns:

- event kind: `text`, `photo`, `voice`, `document`, `command`, `reaction`;
- persisted display text;
- whether the event updates `last_user_message`;
- whether it should enter the live SDK session;
- whether the synthetic prompt should be persisted;
- whether an episode/callback summary should be created;
- which downstream runtime entrypoint is legal.

Then make each Telegram handler describe the event and let the service perform
the state mutation. The value is not just deduplication; it is one place to test
conversation semantics.

Suggested invariant tests:

- Every owner-visible inbound event writes exactly one ledger row.
- Every inbound event type has an explicit `updates_last_user_message` policy.
- Synthetic prompt text never appears in `messages`.
- Every media/document event has an explicit episode policy.
- Reactions either intentionally count as recent user activity or intentionally
  do not, with a test named after that decision.

### P1 - Durable side effects are mixed with best-effort background tasks

The code uses `asyncio.create_task(...)` for several classes of work with very
different durability expectations. Some are genuinely ephemeral UI niceties.
Others look like state convergence, memory indexing, reflection, or background
task execution.

Evidence:

- `tools/memory/remember.py:46-55` writes the canonical SQLite fact, then
  fire-and-forgets the Graphiti/Kuzu episode write.
- `agents/reflection.py:131-143`, `agents/reflection.py:155-167`, and
  `agents/reflection.py:656-668` do the same for reflection-created facts.
- `tools/dispatch/_shared.py:212-222` persists a background task row, then
  starts the actual worker with `asyncio.create_task(...)`.
- `agents/background_listener.py:174-190` explicitly cannot resume a background
  task after restart, even though an SDK session id and task metadata are
  persisted.
- `agents/background_listener.py:135` starts post-task reflection with
  `asyncio.create_task(...)`.
- `agents/sdk_pool.py:275-303` schedules live client recycle as an async task.

Impact:

The same programming primitive currently means "optional side effect",
"durable index update", "background job", "post-task reflection", and "SDK
lifecycle management". On process exit, cancellation, or event loop shutdown,
the user-visible blast radius differs by call site. The architecture does not
name those differences.

Recommendation:

Introduce a durability taxonomy and enforce it in code review/tests:

- `ephemeral`: okay to lose, no DB row required.
- `best_effort_logged`: okay to lose, but failures should be visible.
- `durable_outbox`: must eventually converge; write an outbox row in the same
  transaction as the source mutation.
- `resumable_job`: must be restartable from persisted state.
- `lifecycle_task`: owned by application startup/shutdown, not by a random
  handler.

Graphiti writes are the clearest `durable_outbox` candidate if the graph is
intended to become real memory. Dispatch is either a true `resumable_job` or the
stored session id should be treated as diagnostic metadata, not a promise.

Suggested invariant tests:

- No canonical state mutation schedules a durable side effect without an outbox
  row.
- Restart recovery has a declared behavior for every `bg_tasks.status`.
- Every `create_task(...)` in production code is annotated or wrapped by a
  helper that encodes its durability class.

### P1 - Approval policy is still split across two approval architectures

The first review called out the split between legacy defer and gatekeeper. The
deeper issue is that tests now encode both split paths as independent truths
rather than one approval invariant with two implementations.

Evidence:

- `tests/test_approval_matrix.py:83-108` tests `_is_defer_gated()` and the
  single-tier `CONFIRM-SEND` defer path.
- `tests/test_destructive_tool_gating.py:178-218` asserts Apple Events writes
  deliberately do not trigger the defer hook.
- `tests/test_destructive_tool_gating.py:224-258` asserts
  `gmail_bulk_delete_messages` migrated from defer to gatekeeper and must not
  appear in defer patterns.
- `tests/test_gatekeeper.py:42-330` tests the separate gatekeeper lifecycle:
  approve, reject, expiry, restart recovery, race handling, and audit rows.
- `tests/test_gatekeeper_integration.py:53-187` tests SDK `can_use_tool`
  behavior separately from defer behavior.
- `tools/approvals.py:218-270` has to branch between gatekeeper rows, deferred
  rows, and legacy callback rows inside one reply resolver.

Impact:

The suite protects known behavior, but it does not answer the architectural
question: "Given a tool and args, what owner-approval state machine should
execute?" That makes it easy for a future tool to land in the wrong path or no
path, especially as registry metadata grows.

Recommendation:

Create one `ApprovalPolicy` resolver with a typed result:

```python
ApprovalDecision(
    gate_kind="none" | "defer" | "gatekeeper",
    approval_tier=...,
    timeout_s=...,
    confirmed_tool=...,
    taint_policy=...,
)
```

Both SDK hooks should ask this resolver. The old defer implementation and the
new gatekeeper implementation can remain separate executors for a while, but
they should not independently decide policy.

Suggested invariant tests:

- Every state-changing tool resolves to exactly one approval policy:
  `none`, `defer`, or `gatekeeper`.
- No tool is both defer-gated and gatekeeper-gated.
- Approval reply resolution is scoped by approval kind, not just oldest pending
  row in chat.
- Apple Events ungated behavior is represented as explicit policy metadata, not
  only as comments and negative regex tests.

### P1 - Utility tool auto-discovery bypasses the policy registry

The registry direction is good, but utility tools remain a second path: the
runtime appends discovered utility tool names to the SDK allowlist even if those
tools do not have explicit registry metadata.

Evidence:

- `tools/_tools_yaml.py:122-140` says utility tool names are not included in the
  YAML registry and are appended by the caller.
- `agents/runtime.py:167-174` loads YAML tools, then appends
  `discover_utility_tool_names()` results.
- `tools/_registry.py:107-127` derives fully qualified names for every
  discovered utility tool.
- `tests/test_tools_yaml.py:110-118` explicitly allows runtime
  `allowed_tool_names()` to append utility-auto-discovered names on top.
- `tests/test_tools_yaml.py:167-173` only checks that `python_run` is defer
  gated; it does not require all utility writes to have metadata.

Local policy check from this review found auto-discovered utility tools without
explicit `config/tools.yaml` coverage, including state-changing tools such as
`mcp__hikari_utility__reminder_create`, `mcp__hikari_utility__reminder_cancel`,
`mcp__hikari_utility__receipt_add`, `mcp__hikari_utility__receipt_delete`,
`mcp__hikari_utility__link_save`, `mcp__hikari_utility__link_update`, and
`mcp__hikari_utility__link_delete`.

Impact:

This is not the same as "all of those tools must be owner-gated." Some are
intentionally low-friction personal tools. The architectural problem is that
allowlisting and policy classification are not the same operation. A tool can
be callable without declaring whether it is read-only, local-write,
external-write, destructive, untrusted-output, approval-gated, or audit-worthy.

Recommendation:

Keep auto-discovery for server construction, but require registry metadata for
policy. One practical shape:

- discovered but unregistered utility tools are allowed only in development or
  fail a CI policy test;
- each utility tool declares `capability`, `mutates`, `scope`, `gate`,
  `audit`, and `untrusted_output`;
- `allowed_tool_names()` is generated from the same resolved policy graph used
  by hooks and inventory.

Suggested invariant tests:

- Every discovered utility tool has a registry policy entry.
- Every mutating utility tool declares whether it is gated and why.
- Tool inventory, SDK allowlist, defer hook, gatekeeper hook, and untrusted
  wrapper all read the same resolved registry object.

### P2 - Internal control prompts are doing both reasoning and transport

The three runtime entrypoints are a good split. The weak point is what happens
inside `run_internal_control()`: it is used for legitimate model-only work, but
also for deterministic fetch/mutate jobs that could be typed adapters.

Evidence:

- `agents/proactive.py:419-433` asks the model to delegate to Google Calendar
  and return strict YAML for calendar heartbeat fetch.
- `agents/daily_checkin.py:297-333` asks the model to perform Gmail queries and
  return strict YAML.
- `agents/daily_checkin.py:372-394` asks the model to fetch calendar events and
  return strict YAML.
- `agents/proactive.py:735-777` asks the model to create Apple reminders and
  parse an `event_id` from YAML or prose.
- `agents/proactive.py:804-848` asks the model to create Google Calendar mirror
  events and parse an `event_id` from YAML or prose.
- `tools/approvals.py:358-429` asks the model to replay an approved deferred
  tool call.

Impact:

The stateless entrypoint protects the live session, which is important. But
using the model as a transport adapter adds cost, malformed-output handling,
prompt-injection surface, and a second planner for operations whose desired
behavior is already known. The parsing fallbacks are evidence that the code is
defending against a layer that does not need to be probabilistic.

Recommendation:

Classify internal-control work into:

- `compose`: model should write text;
- `judge`: model should score/classify;
- `fetch`: typed adapter should call an API and return typed data;
- `mutate`: typed adapter should perform a known side effect;
- `replay`: deterministic approved tool execution.

Keep `run_internal_control()` for `compose` and `judge`. Move `fetch`,
`mutate`, and `replay` toward direct adapter functions or MCP client calls with
typed return schemas. Calendar heartbeat, daily check-in fetches, reminder
mirrors, and defer replay are the main candidates.

Suggested invariant tests:

- Fetch/mutate jobs do not parse LLM prose for required IDs.
- Approved replay executes the approved tool args exactly once or remains
  pending.
- Calendar/Gmail fetch functions can be tested without an LLM stub.

### P2 - Graph memory has no declared source-of-truth or path contract

Graphiti/Kuzu currently has two ambiguities: read-side integration and storage
location.

Evidence:

- `tools/memory/recall.py:33-60` reads only from `storage.retrieval.retrieve()`.
- `agents/telegram_bridge.py:1369-1406` exposes `/memory_diff` as a manual
  side-by-side comparison between SQLite recall and Graphiti search.
- `tests/test_graph_phase_c.py:90-106` asserts `add_episode_safe()` swallows
  graph failures.
- `tests/test_graph_phase_c.py:160-192` asserts boot graph failure is non-fatal.
- `storage/graph.py:26-28` stores Kuzu under `Path(os.environ.get("HIKARI_DATA_DIR") or "data")`.
- `storage/db.py:33-34` stores SQLite under the repo-root `data/hikari.db` when
  `HIKARI_DB_PATH` is unset.

Impact:

The graph is currently safe because it is not user-visible recall. But the
manual `/memory_diff` command strongly suggests it is on a path toward recall
integration. Before that happens, the project needs to decide whether SQLite is
the canonical memory and Graphiti is an eventually consistent index, or whether
Graphiti is allowed to become an independent memory store.

The path issue is smaller but concrete: if `HIKARI_DATA_DIR` is unset, graph
storage depends on process cwd, while SQLite defaults to repo-root-relative
storage. That can create two different "data" roots under different launch
contexts.

Recommendation:

- Declare SQLite as canonical until a migration says otherwise.
- Make Graphiti an outbox-fed index with backfill.
- Make `storage/graph.py` use the same repo-root default as `storage/db.py`.
- Keep `/memory_diff` as an operator tool until graph recall has provenance,
  freshness, and fallback semantics.

Suggested invariant tests:

- Default SQLite and Kuzu paths resolve under the same repo-root data directory.
- Normal recall results identify which backend produced each hit once graph is
  integrated.
- Graph failure cannot silently erase canonical memory.

### P2 - Capability health is partly decided at scheduler construction time

Some capabilities are enabled or skipped at process startup rather than through
always-installed jobs that check health each run.

Evidence:

- `agents/scheduler.py:54-75` adds the calendar heartbeat job only if
  `_calendar_creds_healthy()` is true while building the scheduler.
- `agents/scheduler.py:117-133` does the same for Google Calendar reminder sync.
- `agents/scheduler.py:325-347` defines health from
  `runtime_state.calendar_heartbeat_healthy` or OAuth env vars.

Impact:

If credentials are repaired, refreshed, or marked healthy after scheduler
construction, the skipped jobs do not appear until restart. That is okay if
"restart after credential change" is the explicit operational contract. It is
risky if the runtime state flag is meant to be a live health signal.

Recommendation:

Prefer installing the scheduler jobs unconditionally, then gate inside the job
body with a cheap health check. For expensive failing paths, add exponential
backoff or a health table. That gives the system a dynamic capability model
without requiring scheduler reconstruction.

Suggested invariant tests:

- A credential health flip from false to true enables work without process
  restart, or a test asserts restart is required and the operator message says
  so.
- Pending Google Calendar reminder mirrors do not accumulate indefinitely after
  credentials become healthy.

### P2 - SDK client recycle is lifecycle work outside the live-turn lock

The runtime correctly serializes live user/proactive turns through `_RUN_LOCK`.
The SDK pool then schedules client recycle as a separate async task.

Evidence:

- `agents/sdk_pool.py:275-303` increments the live-turn counter and schedules
  `_reconnect_live(...)` via `asyncio.create_task(...)`.
- The comment at `agents/sdk_pool.py:278-280` says it must be called outside
  `_RUN_LOCK`, but the call site is reached from live invocation after a turn.
- `agents/sdk_pool.py:254-259` returns the current live client, reconnecting
  only if `_live.client is None`.

Impact:

This may be fine in practice, especially if the reconnect lock prevents actual
double-connects. The architectural concern is that client lifecycle is not
owned by the same live-turn gate that protects session mutation. A recycle task
can be pending while a new live turn is arriving, and correctness relies on the
internal client/connect locks rather than on one explicit "no recycle during a
live turn" invariant.

Recommendation:

Make the recycle state explicit:

- after a turn, mark `live_recycle_requested=True`;
- before the next live turn acquires/uses the client, perform the recycle under
  the same live-turn serialization boundary; or
- prove with tests that a pending recycle cannot disconnect a client while a
  turn is streaming.

Suggested invariant tests:

- A live recycle request cannot disconnect the active client during a user turn.
- A second live turn arriving during recycle waits for the new client or safely
  uses the old one, but never races both.

## Cross-cutting test gap

The test suite is valuable and full of scar tissue from real incidents. The
deeper gap is that many tests are "this old bug stays fixed" tests, not "this
subsystem obeys one contract" tests.

Examples:

- `tests/test_start_and_reaction_event_rows.py` protects event-row fixes for
  `/start` and reactions, but there is no central matrix for all inbound event
  types.
- `tests/test_destructive_tool_gating.py:178-218` protects Apple Events
  ungated behavior, while other tests protect defer and gatekeeper behavior.
  There is no single approval-policy matrix over the complete registry.
- `tests/test_graph_phase_c.py:90-106` and `tests/test_graph_phase_c.py:160-192`
  prove graph failure is non-fatal, but not that graph writes eventually
  converge when graph is intended to matter.
- `tests/test_allowlist_completeness.py:27-84` protects specific allowlist
  regressions, but not policy completeness for every discovered utility tool.

This is normal for a fast-moving system. The next architecture step is to add a
thin layer of contract tests above the incident regression tests:

- conversation event matrix;
- tool policy matrix;
- approval policy matrix;
- durable side-effect matrix;
- capability health matrix;
- memory source-of-truth matrix.

## Recommended sequencing

1. Build the conversation event matrix first.

   This is the highest-leverage cleanup because it touches user-visible history,
   gap awareness, proactive suppression, callback episodes, and live-session
   selection.

2. Require registry metadata for every utility tool.

   Do this before adding more tools. It is much easier to classify the current
   set than to retrofit policy after another wave of convenience tools lands.

3. Introduce an approval policy resolver.

   Keep defer and gatekeeper executors separate initially, but make one resolver
   decide which executor applies.

4. Add a durable outbox for graph writes.

   This should happen before normal recall uses Graphiti. Until then, treat
   Graphiti as an experimental index.

5. Split internal control into typed adapters versus model jobs.

   Start with calendar/Gmail fetch and reminder mirror paths because they
   already fight strict YAML parsing.

6. Make capability health live or explicitly restart-bound.

   Either behavior is acceptable; the architecture just needs to say which.

## Bottom line

The current system is not lacking components. It is lacking a few small,
authoritative contracts. The next architectural win is not a large rewrite; it
is turning repeated handler-local decisions into typed policy tables and thin
services:

- `ConversationLedger` for inbound events;
- `ToolPolicyRegistry` for all callable tools;
- `ApprovalPolicy` for owner confirmation;
- `DurableOutbox` for eventual side effects;
- `CapabilityHealth` for startup/live dependency status.

Those names do not need to be the final names. The important thing is that each
question has one home.
