# Code Structure Review Findings

Date: 2026-05-19

Scope: deep review of code structure only. This excludes product behavior, tone,
and implementation bugs except where they reveal a structural boundary problem.
Parallel agents reviewed module boundaries, runtime orchestration, and
data/tool layers; one orchestration worker did not return and was closed after
multiple waits, so orchestration findings below are from local inspection.

## Findings

### P1: Approval orchestration forms a cross-package import knot

References:

- `agents/runtime.py:31`
- `agents/runtime.py:37`
- `agents/hooks.py:332`
- `tools/approvals.py:327`
- `tools/wiki.py:26`

`agents.runtime` imports tool modules and hooks to build the SDK runtime, while
`agents.hooks` reaches into `tools.approvals`, and `tools.approvals` lazy-imports
`agents.runtime._run_query` to resume deferred approvals. `tools.wiki` also
imports approvals directly. This creates a structural cycle around the most
sensitive orchestration path: runtime setup, tool gating, approval persistence,
and post-approval execution all know about each other.

Why it matters structurally: approval orchestration is an application/runtime
concern, but it currently lives inside the tool layer and calls back into the
runtime. That makes ownership unclear and forces lazy imports to avoid immediate
cycles.

Preferred shape: move approval workflow orchestration into a neutral application
service, for example `agents/approval_runtime.py` or `services/approvals.py`.
Tool adapters should declare gated operations; the runtime/composition layer
should decide how to defer, resume, and audit them.

### P1: `storage` and `tools` depend on each other

References:

- `storage/retrieval.py:25`
- `tools/memory.py:14`
- `tools/memory.py:15`

`storage.retrieval` imports `tools.embeddings`, while `tools.memory` imports
`storage.db` and `storage.retrieval`. That makes the package dependency graph
bidirectional: storage reaches upward into tools, and tools reach downward into
storage.

Why it matters structurally: retrieval and embeddings are domain/infrastructure
capabilities, not MCP tool concerns. Keeping embeddings in `tools` means lower
layers cannot be reused without importing the tool package.

Preferred shape: move embeddings into a neutral package such as
`services/embeddings.py` or `ml/embeddings.py`. Then `storage.retrieval` and
`tools.memory` can both depend on that neutral module without forming a cycle.

### P2: Shared configuration lives under the `agents` package

References:

- `agents/config.py:1`
- `tools/location.py:25`
- `tools/voice.py:20`
- `tools/budget.py:27`
- `tools/approvals.py:26`
- `mcp_external/server.py:23`
- `mcp_external/launch.py:21`

`agents.config` is used as shared infrastructure by tool modules and the
external MCP package. This forces non-agent layers to import the agent package
for basic configuration access.

Why it matters structurally: the package name says `agents`, but it currently
contains cross-cutting application configuration. This blurs layer direction and
makes `tools` and `mcp_external` look like agent internals even when they are
separate adapters.

Preferred shape: move config to a neutral package such as `core/config.py` or
`hikari/config.py`, then update all layers to import from that neutral location.

### P2: Telegram bridge is doing too many structural jobs

References:

- `agents/telegram_bridge.py:27`
- `agents/telegram_bridge.py:53`
- `agents/telegram_bridge.py:95`
- `agents/telegram_bridge.py:162`
- `agents/telegram_bridge.py:247`
- `agents/telegram_bridge.py:321`
- `agents/telegram_bridge.py:417`
- `agents/telegram_bridge.py:578`
- `agents/telegram_bridge.py:625`

`telegram_bridge.py` owns transport setup, owner authorization, approval
resolution, politeness/affect/belief gates, media download, voice transcription,
location handling, direct DB writes, outgoing filtering/choreography, photo
outbox draining, command handling, scheduler startup, dispatch wiring, and
background recovery.

Why it matters structurally: Telegram-specific code is now the composition root,
handler layer, workflow layer, and part of the domain policy layer. Adding a
second transport or testing inbound flows independently requires threading
through a 662-line bridge module.

Preferred shape: keep Telegram handlers thin. Extract an inbound turn service
for text/photo/voice/location workflows, an outbound sender/filter service, and
a startup composition module that wires bot refs, dispatch, approvals, and
scheduler jobs.

### P2: Tool modules depend on process-global runtime wiring

References:

- `tools/approvals.py:34`
- `tools/approvals.py:38`
- `tools/approvals.py:43`
- `tools/dispatch.py:43`
- `tools/dispatch.py:47`
- `tools/dispatch.py:56`
- `agents/telegram_bridge.py:642`
- `agents/telegram_bridge.py:645`
- `agents/background_listener.py:20`

Approvals store a module-global Telegram bot reference. Dispatch stores a
module-global owner chat id and exports a module-global event queue. The bridge
mutates those globals during startup, while the background listener imports the
queue directly.

Why it matters structurally: startup order becomes part of correctness, and
tool modules are no longer plain adapters. They are partially initialized
runtime singletons. This also makes tests rely on monkeypatching module state.

Preferred shape: introduce an explicit app context or service container passed
to the composition layer. Tools should receive dependencies through factories or
small service interfaces instead of module globals.

### P2: Internal jobs share the main conversation session boundary

References:

- `agents/runtime.py:188`
- `agents/runtime.py:205`
- `agents/runtime.py:223`
- `agents/runtime.py:236`
- `agents/runtime.py:242`
- `agents/proactive.py:201`
- `agents/proactive.py:322`
- `agents/reflection.py:94`
- `agents/reflection.py:303`

`_run_query` always reads and persists the shared `session_id`. The public chat
entrypoint, proactive generation, calendar fetch, reflection, and session
consolidation all go through the same helper. `log_to_memory=False` prevents
some message logging, but it does not create a separate Claude session boundary.

Why it matters structurally: internal maintenance prompts and user-facing chat
turns are different workflows. Sharing one resumable session makes the boundary
implicit and pushes isolation into caller convention.

Preferred shape: split runtime calls by run kind. User chat can resume the main
session; proactive, reflection, calendar fetch, and maintenance calls should use
isolated sessions or explicit per-kind session keys.

### P2: Deferred approval execution is still model-mediated runtime work

References:

- `tools/approvals.py:251`
- `tools/approvals.py:291`
- `tools/approvals.py:334`
- `agents/runtime.py:128`
- `agents/runtime.py:132`
- `agents/runtime.py:135`

After approval, `_resume_after_defer` builds a synthetic prompt and asks the
model to call the confirmed sibling tool. `_build_options` starts from the base
tool allowlist and extends it with the confirmed tool for that turn.

Why it matters structurally: an approved deterministic side effect is routed
back through a broad conversational runtime. The code has two concepts tangled
together: "continue the agent turn" and "execute the approved operation."

Preferred shape: if the goal is deterministic post-approval execution, call the
confirmed adapter directly from the approval service and then optionally notify
the runtime with the result. If the goal is resumable model interaction, build a
narrow approval-resume runtime with only the confirmed tool attached.

### P2: `runtime_state` is used as a shared mutable state bus

References:

- `storage/db.py:1024`
- `storage/db.py:1030`
- `agents/bridge_ux.py:75`
- `agents/reactions.py:51`
- `agents/stickers.py:61`
- `agents/cadence.py:83`
- `agents/proactive.py:154`
- `agents/proactive.py:360`
- `tools/budget.py:47`
- `tools/photos.py:75`

Many modules encode counters, caps, dedup keys, windows, and feature state as
string values in `runtime_state`. Several are read-modify-write sequences spread
across modules.

Why it matters structurally: `runtime_state` has become a generic cross-module
state bus. That hides ownership of state transitions and makes concurrency,
expiry, and invariants hard to reason about.

Preferred shape: keep `runtime_state` for simple key/value flags, but move
high-churn counters and windows into typed helper APIs or dedicated tables with
atomic update methods, for example `increment_runtime_counter`,
`append_proactive_log`, and `mark_calendar_notified`.

### P2: Dispatch persistence does not match dispatch lifecycle ownership

References:

- `tools/dispatch.py:4`
- `tools/dispatch.py:114`
- `tools/dispatch.py:147`
- `agents/background_listener.py:165`
- `agents/telegram_bridge.py:522`
- `agents/telegram_bridge.py:544`

Dispatch stores task rows and SDK session ids, but lifecycle control remains
in-process. Restart recovery marks running tasks failed rather than resuming
them. `/cancel` updates the row, but the in-process worker can keep running and
later write completion state.

Why it matters structurally: the database schema implies durable background
work, while execution semantics are in-memory best effort. Persistence and
lifecycle ownership are not aligned.

Preferred shape: choose one lifecycle model. Either make dispatch explicitly
ephemeral and simplify the persisted state, or introduce a task runner that owns
resume/cancel semantics and checks task status before writing final completion.

### P2: Packaging metadata omits a real package and runtime dependencies

References:

- `pyproject.toml:37`
- `mcp_external/launch.py:5`
- `mcp_external/server.py:21`
- `mcp_external/launch.py:99`

`mcp_external` has tests and a documented `python -m mcp_external.launch`
entrypoint, but the wheel target packages only `agents`, `storage`, and
`tools`. The external MCP path also imports `mcp` and `uvicorn`, but those
dependencies are not listed in `pyproject.toml`.

Why it matters structurally: the source checkout and installed distribution do
not have the same module boundary. That makes deployment shape depend on running
from the repo rather than the package.

Preferred shape: include `mcp_external` in the wheel packages and declare its
runtime dependencies, or explicitly mark it as a dev/local-only component and
remove it from packaged expectations.

### P3: MCP response envelope is duplicated across adapters

References:

- `tools/memory.py:18`
- `tools/wiki.py:82`
- `tools/dispatch.py:68`
- `tools/approvals.py:76`
- `mcp_external/server.py:82`

Multiple tool modules define identical `_ok()` helpers returning the MCP-style
`{"content": [{"type": "text", "text": ...}], "data": ...}` envelope.
`mcp_external.server` then indexes directly into that internal response shape.

Why it matters structurally: the response contract is not owned by one module.
Changing the envelope requires editing unrelated adapters and external wrappers.

Preferred shape: create a shared `tools/response.py` or neutral
`core/mcp_response.py` with helpers for building and extracting tool text/data.

### P3: Hardcoded local roots are spread through tool and agent definitions

References:

- `tools/wiki.py:30`
- `tools/dispatch.py:50`
- `tools/dispatch.py:195`
- `agents/subagents.py:84`
- `agents/subagents.py:91`

The Obsidian vault path and work directory root are hardcoded in tool modules
and repeated in subagent prompt text.

Why it matters structurally: deployment-specific paths are mixed into adapter
logic and model-facing instructions. Moving the project to another user, host,
or test fixture requires editing code and prompts.

Preferred shape: put path roots in config and render subagent prompts from the
same source, so tool validation and model instructions cannot drift.

## Structural Priorities

1. Break the import cycles around runtime, hooks, tools, and approvals.
2. Move shared infrastructure (`config`, `embeddings`, MCP response helpers) out
   of feature-specific packages.
3. Split Telegram bridge into transport handlers plus workflow/services.
4. Give internal jobs separate runtime/session boundaries.
5. Replace module-global wiring with an explicit composition context.

No tests were run for this review because no code behavior was changed.
