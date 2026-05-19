# Agent System Architecture Review

Date: 2026-05-19

Scope: review the local `hikari-agent` architecture, compare it against current
agent-system practice, and turn the result into concrete implementation
suggestions.

## Executive Summary

The architecture is directionally right: one lead agent, narrow specialist
subagents, deterministic Python gates, explicit memory tables, write approvals,
prompt-injection wrapping, and persona drift telemetry. That is closer to what
strong agent builders are converging on than a generic "swarm" framework would
be.

The biggest gaps are operational, not aesthetic:

1. There is no first-class run trace that can replay or grade a full agent turn.
2. There is no eval harness that measures full-turn behavior against frozen
   scenarios.
3. Background dispatch is not truly durable. Restart/cancel semantics are still
   partial.
4. Tool risk is uneven. Wiki writes are gated well, but `dispatch_claude_session`
   can grant write/bash capability in repos without approval, and not all
   untrusted external tool outputs are wrapped through one generic boundary.
5. Several LLM-output contracts rely on prompt wording plus YAML/string parsing
   instead of typed schemas with hard fallbacks.

The best next move is not "add more agents." It is to build a trace/eval spine
and make tool boundaries more explicit. Attention mechanisms still make sense;
agent swarms mostly don't until the instrumentation exists.

## Current Architecture Map

### Entry And Runtime

- Telegram is the main user interface: `agents/telegram_bridge.py`.
- A single owner lock blocks non-owner Telegram users.
- `agents/runtime.py` creates one fresh `ClaudeSDKClient` per turn and resumes
  the prior Claude session id from SQLite.
- `_RUN_LOCK` serializes all resumed SDK calls so concurrent user/proactive/defer
  turns do not fork the shared session.
- Primary model and fallback are environment-controlled:
  `claude-sonnet-4-6` and `claude-haiku-4-5`.
- Main runtime allowlist includes `Agent`, memory tools, photo generation, wiki
  tools, dispatch, and read/search tools.

This is a good baseline. The single lead-agent loop keeps the relationship state
coherent, while the lock avoids one of the easiest session-resume failure modes.

### Context And Memory

- SQLite owns persistent memory in `storage/db.py`.
- Memory types include `core_blocks`, `facts`, `episodes`, `tasks`,
  `character_thoughts`, `runtime_state`, `lexicon`, `observations`,
  `noticings`, `peer_representation`, `persona_drift_scores`, `approvals`,
  `audit_log`, and `background_tasks`.
- `agents/hooks.py` injects always-on context:
  core blocks, peer representation, affect state, open tasks, lexicon, location,
  observations, noticings, and session handoff.
- Retrieval moved out of automatic per-turn injection into an on-demand recall
  subagent.
- `storage/retrieval.py` now implements hybrid vector plus BM25 retrieval.
  `README.md` still says `sqlite-vec` is deferred, which is stale.

The on-demand recall move is the right shape. Dumping top-k memory every turn
usually increases sycophancy and hallucinated continuity. Retrieval should be a
tool with calibration, not ambient soup.

### Subagents

`agents/subagents.py` defines six specialists:

- `recall`: Haiku memory specialist, calls only memory recall.
- `wiki`: Haiku wiki specialist, reads/appends wiki through gated tools.
- `code_dispatch`: Haiku dispatcher that spawns long-running Claude Code work.
- `drive_gmail`: Google Workspace specialist.
- `notion`: Notion specialist.
- `research`: Sonnet web research specialist.

This is better than peer-agent chatrooms. The subagents have bounded tool scopes
and return raw material for the lead to rewrite.

### Tools And Side Effects

- Memory tools are in-process MCP tools under `tools/memory.py`.
- Wiki read/write lives in `tools/wiki.py`.
- Wiki append has a public gated tool and a hidden post-approval confirmed tool.
- `tools/approvals.py` persists approval rows and resumes deferred SDK calls.
- `tools/dispatch.py` can spawn nested Claude Code sessions under
  `/Users/alt/work_dir`.
- `mcp_external/server.py` exposes five read-only memory/wiki-search tools over
  external MCP with bearer auth and audit logging.

The wiki approval design is strong. The confirmed tool is attached only during
the approval-resume turn, so the normal tool manifest does not expose the final
write primitive.

The dispatch tool is the dangerous outlier. It gives the model a path to
`Read,Edit,Write,Bash,Glob,Grep` in any repo under `/Users/alt/work_dir`, with
no approval gate. That is useful, but it should be treated as a high-impact
capability.

### Proactive System

- APScheduler runs heartbeats, re-engagement, calendar prep, consolidation, and
  daily reflection from `agents/scheduler.py`.
- `agents/proactive.py` uses deterministic gates for quiet hours, recent user
  activity, cadence, trigger source, and silence windows. Sonnet only writes the
  final proactive message.
- `agents/cadence.py` caps proactive sends per seven days and requires a
  justified trigger source.

This split is correct: Python decides whether to speak, the model only writes
what to say.

One issue: `_pick_seed()` defaults `source` to `recent_episode_callback` even
when there may be no actual recent episode. That weakens the "justified source"
contract. If no concrete source exists, heartbeat generation should skip.

### Safety, Security, And Voice Hardening

- `agents/post_filter.py` blocks canary leaks, detects assistant-safety voice,
  and detects sycophancy/anchor violations.
- `agents/injection_guard.py` wraps untrusted wiki/external content in
  delimiters and uses a canary tripwire.
- `agents/log_scrub.py` redacts secrets and escalates canary leakage.
- `agents/drift_judge.py` samples outbound replies with a cheap judge model and
  writes scores to `persona_drift_scores`.
- `agents/belief_frame.py`, `agents/politeness_gate.py`, and `agents/affect.py`
  handle targeted deterministic behavior gates.

The direction is unusually good for a companion agent. Most projects leave voice
as a prompt and then wonder why it rots. Here, voice has filters, tests, and
telemetry.

The main weak point is enforcement. `filter_outgoing()` can mark
`needs_llm_rewrite`, but the send path mostly logs long-form refusal/sycophancy
leaks instead of rewriting or hard-failing. Detection without an enforcement mode
is still useful, but it is not the final form.

## Internet Research Synthesis

### What Strong Agent Builders Converge On

Anthropic's "Building effective agents" argues for starting with the simplest
system that works, then increasing agency only when needed. Their recommended
patterns are mostly deterministic workflows around model calls: prompt chaining,
routing, parallelization, orchestrator-worker, and evaluator-optimizer. Agents
are framed as useful when the model must decide its own tool path over multiple
steps, not as the default architecture.

Source: https://www.anthropic.com/engineering/building-effective-agents

Anthropic's multi-agent research report says multi-agent systems help especially
when work naturally decomposes into parallel breadth-first exploration. It also
warns that multi-agent systems burn more tokens and are often worse for tasks
where context must remain centralized, such as many coding tasks.

Source: https://www.anthropic.com/engineering/built-multi-agent-research-system

OpenAI's Agents SDK docs emphasize the production pieces around agents:
tools, handoffs, guardrails, tracing, and evals. The important part is the
system boundary, not the word "agent."

Sources:

- https://platform.openai.com/docs/guides/agents
- https://platform.openai.com/docs/guides/evals
- https://platform.openai.com/docs/guides/evaluation-best-practices

LangGraph's docs emphasize durable execution, persistence, human-in-the-loop
control, memory, and streaming. The useful takeaway is not "switch frameworks";
it is that long-running agents need explicit checkpoints and resumable state.

Sources:

- https://langchain-ai.github.io/langgraph/
- https://langchain-ai.github.io/langgraph/concepts/durable_execution/

The 12-Factor Agents community pattern is mostly about owning the control flow:
own the prompts, own the context window, own the tool loop, make tool calls
structured, make state explicit, and treat human contact as a tool. That maps
well to this repo because it is already code-first.

Source: https://github.com/humanlayer/12-factor-agents

Simon Willison's "lethal trifecta" framing is still the clearest security model
for agentic tools: untrusted content plus private data plus outbound side effects
creates prompt-injection exfiltration risk. This repo already recognizes that,
but the boundary should be made generic across every external content tool.

Source: https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/

### Research Takeaways For This Repo

- Keep the lead-agent architecture. It is right for a single-user companion
  because relationship continuity is centralized.
- Keep specialists narrow. Add agents only for bounded domains with disjoint tool
  scopes.
- Do not build a peer swarm for chat, memory, or personality. It will make
  state coherence worse.
- Put deterministic code around the model wherever the desired behavior is
  checkable: approvals, cadence, quiet hours, validation, retry, parsing,
  tracing, cost caps, and evals.
- Treat every tool schema as part of the user interface for the model. Tool
  naming, examples, bounds, enums, and side-effect descriptions matter.
- Make traces and evals load-bearing. Without them, every change to memory,
  persona, tools, or proactive behavior is regression roulette.

## Recommendations

### P0: Add A First-Class Agent Run Trace

Create tables like:

- `agent_runs`: id, kind, source, session_id_before, session_id_after, model,
  prompt_hash, prompt_preview_redacted, started_at, completed_at, status,
  total_cost_usd, total_tokens, final_text_hash, final_text_preview_redacted.
- `agent_events`: run_id, seq, event_type, actor, tool_name, subagent_name,
  payload_json_redacted, artifact_hash, started_at, completed_at, status,
  error_preview.

Record events for:

- hook context blocks injected;
- recall queries and returned memory ids;
- subagent calls and outputs;
- tool use start/end;
- approval defers/resumes;
- post-filter hits;
- drift judge samples;
- proactive gate decisions;
- final outbound text preview/hash.

Do not store full secrets or raw private payloads by default. Store redacted
previews plus hashes, and optionally enable local full payload capture behind a
config flag.

Why this matters: current logs and audit rows are useful but fragmented. You
cannot replay a turn, grade a failed behavior, or compare two architecture
changes without reconstructing state by hand.

### P0: Build Scenario Evals Before Adding More Agency

Add `evals/` with YAML scenarios and a runner, for example:

```yaml
id: recall_low_confidence_blank
initial_db:
  facts: []
user: "remember what i said about the paper?"
assertions:
  - no_tool: "mcp__hikari_memory__remember"
  - reply_matches_any:
      - "blanking"
      - "remind me"
  - persona_no_assistant_voice: true
```

Start with these suites:

- Recall calibration: high/medium/low confidence, contradiction search, stale
  facts, superseded facts.
- Memory writes: only stable facts, conflict handling, no duplicate facts,
  no accidental always-on bloat.
- Tool safety: wiki read injection, wiki append approval, dispatch approval,
  Gmail/Notion write gating.
- Proactive: no message during quiet hours, no source-less heartbeat, no repeated
  open-loop nagging, calendar dedupe.
- Persona: no assistant voice, no banned task-solicitation endings, no anchor
  surrender, no sycophancy collapse.
- Multimodal: photo/voice prompts remain short and do not over-remember.

Use two layers:

- Fast deterministic tests with fake SDK/model responses.
- A small nightly live-model eval set with frozen DB fixtures and judge rubrics.

The repo already has good unit tests. It needs full-turn behavioral evals.

### P0: Gate `dispatch_claude_session`

Treat dispatch as Tier-1 or Tier-2 approval by default. It can spawn a coding
agent with write/bash tools in local repos, so it belongs in the same safety
class as other meaningful side effects.

Suggested shape:

- Replace raw `allowed_tools` with named presets:
  `read_only`, `edit_no_bash`, `edit_with_tests`, `full_code`.
- Default to `read_only` unless the user's current turn explicitly asks for
  implementation.
- Require approval for any preset containing `Write`, `Edit`, or `Bash`.
- Store repo path, tool preset, and max budget in the approval summary.
- Add prompt-injection tests proving untrusted wiki/web/email content cannot
  trigger dispatch.

This is the largest current blast-radius issue.

### P0: Make Background Work Truly Durable

`background_tasks` persists task state, but the actual running session is an
in-process `asyncio.Task`. Restart recovery can notify about stale work, but it
does not fully resume the worker loop. `/cancel` only marks a row as cancelled;
the nested SDK client keeps running until it ends.

Implement a durable worker state machine:

- queued -> leased -> running -> done/failed/cancelled;
- `lease_owner`, `lease_expires_at`, `heartbeat_at`, `attempt`;
- worker loop scans queued and stale running tasks on startup;
- each task resumes from stored `session_id`;
- cancellation checks the DB before starting each new SDK turn or tool-heavy
  step;
- cancelled tasks are not reflected into long-term memory.

If SDK cancellation cannot interrupt an active model call, say that in the code
and make cancellation "stop after current SDK response." Right now the user
message says that, but the architecture should encode it as a state contract.

### P0: Turn Untrusted-Content Handling Into A Generic Boundary

Right now `wiki_read` and external MCP wrap content. The config lists Google,
research, browser, and other untrusted tools, but there is no single generic
PostToolUse wrapper that applies to every matching tool.

Add a hook or proxy layer that:

- detects configured untrusted tool names;
- wraps returned text/content with `wrap_untrusted`;
- records untrusted artifact ids, URLs, and content hashes for the active run;
- passes those ids into approval/audit flagging if outbound tools use them;
- tests delimiter escape, canary non-leakage, and outbound blocked states.

The goal is one invariant: any content from outside the trusted local state is
data until deliberately transformed.

### P1: Enforce Or Remove The LLM Rewrite Path

`filter_outgoing()` can return `needs_llm_rewrite`, but the bridge generally
logs long-form leaks and ships them unless short replacement fires. Pick one:

- implement a single bounded rewrite retry with `max_turns=2`, no tools, no
  session memory write until accepted; or
- disable the rewrite flag and use deterministic fallback for high-risk leaks.

For companion behavior, "detect and log" is acceptable during development but
not for production. The code already knows when the reply broke character.

### P1: Type Subagent Contracts

Several contracts are prompt-only:

- recall output must start with exact confidence tokens;
- calendar fetch must return strict YAML;
- reflection must return strict YAML;
- drift judge must return strict YAML;
- research returns prose with URLs.

Move these to typed parse/validate helpers:

- Pydantic or dataclass validators for internal records.
- Retry once on parse failure with the parse error included.
- On second failure, return a safe empty result and write a trace event.
- Add unit tests for malformed YAML, missing fields, extra prose, and nulls.

Prompt discipline is not a parser. Annoying but correct.

### P1: Tighten Memory Quality Controls

Specific changes:

- Fix `README.md`: retrieval is no longer BM25-only; it uses sqlite-vec when
  embeddings are available.
- Add an embedding backfill scheduler or startup health check for facts/episodes
  without embeddings.
- Add retrieval eval fixtures for exact, semantic, stale, and contradiction
  queries.
- Add "why retrieved" metadata: source signal, vector distance, BM25 rank,
  age, importance, superseded status.
- In daily reflection, do not only compare against the latest 20 active facts.
  For each candidate fact, query active facts by subject/predicate and likely
  aliases before deciding whether to add/supersede.
- Add a memory quarantine state for low-confidence extracted facts instead of
  inserting them as active memory immediately.

The current memory layer is promising, but long-term companion quality depends
more on precision than recall. Bad memories are worse than missing memories.

### P1: Make Proactive Source Proof Explicit

Change `_pick_seed()` so it returns no heartbeat candidate unless there is a
real source object:

- open task id;
- observation id;
- noticing id;
- lexicon id;
- recent episode id;
- calendar event id;
- re-engagement silence gap id.

Store that source in `runtime_state` or the new trace tables when a proactive
message is sent.

This turns the cadence governor from "source-shaped label" into actual evidence.

### P1: Improve Tool ACI

Tool descriptions should be treated as model-facing UI. Update high-impact
tools with:

- explicit side effects;
- examples of good/bad calls;
- bounds and defaults;
- enums instead of free-form strings;
- idempotency expectations;
- when not to call.

Start with:

- `dispatch_claude_session`;
- `remember`;
- `task_create`;
- `wiki_append`;
- Google/Notion write tools if they become available through MCP.

The raw comma-separated `allowed_tools` string in dispatch is especially weak.
Use typed presets.

### P1: Add Operational Runbooks

Add `codex/operations_runbook.md` later with:

- environment variables and secret source;
- launchd/service commands;
- DB backup and restore;
- schema migration procedure;
- how to inspect pending approvals and background tasks;
- how to run evals;
- how to rotate the external MCP bearer token;
- how to recover from bad memory insertion;
- how to disable proactive messages quickly.

This project has enough moving pieces that "read the code" is no longer a
reasonable recovery plan.

### P2: Add Schema Migrations Instead Of Only Idempotent Sniffs

The current schema setup is pragmatic and testable, but as the DB grows, add a
`schema_migrations` table with explicit versions. Keep idempotent checks for
defense, but record which migrations ran.

Benefits:

- easier production debugging;
- safer future destructive migrations;
- visible migration history;
- less dependence on process-level `_SCHEMA_INITIALIZED` behavior.

### P2: Make Observability Spans Real

`agents/observability.py` is currently optional/no-op unless configured. Once
run traces exist, wire spans around:

- Telegram handler start/end;
- `_run_query`;
- each hook;
- each tool call;
- each subagent call;
- each scheduler job;
- each approval defer/resume;
- each background dispatch task.

This can go to Logfire or stay local. The important thing is a common run id.

### P2: Split Persona Policy From Operational Policy In Prompts

The persona file is huge and load-bearing. That is fine for the lead agent, but
operational instructions should be easier to audit:

- persona/voice;
- memory policy;
- tool policy;
- safety/security policy;
- proactive policy;
- subagent delegation policy.

The code already separates some of this through config and skills. The next step
is making the prompt stack modular enough that eval failures can point to one
layer instead of the whole persona blob.

Do not make this a refactor-first project. Do it when adding the trace/eval
spine so changes are measurable.

## Suggested Implementation Order

1. Add `agent_runs` and `agent_events`, then wire minimal tracing through
   `_run_query`, hooks, tools, approvals, and proactive gates.
2. Add eval scenarios for the current behavior before changing behavior.
3. Gate `dispatch_claude_session` behind approval and replace raw tool strings
   with presets.
4. Add a generic untrusted-output wrapper for all configured external tools.
5. Enforce the post-filter rewrite/fallback path.
6. Make proactive source proof explicit.
7. Add typed output validators for reflection, calendar fetch, recall, and
   drift judge.
8. Add durable worker leases for background dispatch.
9. Fix README drift and write the operations runbook.

## Files Most Affected

- `agents/runtime.py`: run tracing, tool wrapping hook, no-tools rewrite calls.
- `agents/hooks.py`: untrusted tool wrapping, approval/defer event tracing.
- `agents/telegram_bridge.py`: outbound filter enforcement, trace ids.
- `agents/proactive.py`: explicit proactive source proof.
- `tools/dispatch.py`: approval gating, tool presets, durable execution hooks.
- `tools/approvals.py`: trace/audit integration, longer-lived approval UX.
- `storage/db.py`: run/event tables, schema migrations, durable leases.
- `storage/retrieval.py`: retrieval metadata and eval support.
- `agents/reflection.py`: typed parsing and memory quarantine.
- `tests/`: scenario eval harness plus dispatch/injection/proactive regressions.

## Research Sources

- Anthropic, "Building effective agents":
  https://www.anthropic.com/engineering/building-effective-agents
- Anthropic, "How we built our multi-agent research system":
  https://www.anthropic.com/engineering/built-multi-agent-research-system
- OpenAI Agents guide:
  https://platform.openai.com/docs/guides/agents
- OpenAI Evals guide:
  https://platform.openai.com/docs/guides/evals
- OpenAI evaluation best practices:
  https://platform.openai.com/docs/guides/evaluation-best-practices
- LangGraph overview:
  https://langchain-ai.github.io/langgraph/
- LangGraph durable execution:
  https://langchain-ai.github.io/langgraph/concepts/durable_execution/
- 12-Factor Agents:
  https://github.com/humanlayer/12-factor-agents
- Simon Willison, "The lethal trifecta for AI agents":
  https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/
