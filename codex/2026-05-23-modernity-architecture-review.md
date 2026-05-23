---
title: Hikari Agent Modernity and Architecture Review
date: 2026-05-23
repo: /Users/ol/agents/hikari-agent
reviewer: Codex
---

# Hikari Agent Modernity and Architecture Review

## Findings

### P1: External MCP OAuth is behind the current MCP authorization spec

The external MCP server implements OAuth 2.1, PKCE, open DCR, refresh rotation,
and protected-resource discovery, which is the right general direction. The
gap is the latest MCP authorization spec's resource/audience requirement:
clients must send `resource` in both authorization and token requests, and MCP
servers must validate access tokens as issued for their own resource.

Current code accepts and stashes `response_type`, `client_id`, `redirect_uri`,
`code_challenge`, `code_challenge_method`, `state`, and `scope`, but not
`resource` (`mcp_external/oauth.py:324-386`). The token exchange also validates
code, verifier, client id, and redirect URI, but not `resource`
(`mcp_external/oauth.py:516-545`). Request middleware accepts any unexpired
local access token without checking audience/resource (`mcp_external/launch.py:126-136`).

Impact: current clients may still work, but this is a spec drift risk. It also
becomes a real security issue if this issuer ever fronts more than one MCP
resource, because tokens are not audience-bound.

Fix direction:

- Add `resource` to auth-code and token persistence.
- Require or at least accept `resource` on `/authorize` and `/token`, depending
  on the client compatibility target.
- Validate it against the canonical public resource URI, probably
  `mcp_external.public_base_url` and/or the mounted MCP path.
- Include `scope` in the 401 `WWW-Authenticate` challenge if scopes become
  more granular.

Source: MCP Authorization, latest spec version 2025-11-25:
https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization

### P1: The advertised quality gate is not actually green

`uv run python -m pytest -q` is healthy: 1148 passed, 18 skipped, 2 warnings.
`uv run python scripts/validate_tool_registry.py` is also clean.

But `uv run ruff check .` fails with 262 lint errors. Most are low-risk
formatting/import modernization issues, but the repo README explicitly lists
`uv run ruff check .` as a verification command (`README.md:112-114`), and the
repo has Ruff configured in `pyproject.toml:49-54`.

Impact: this erodes confidence in future reviews because "run the checks" has
two meanings: tests pass, lint does not. It also makes automated formatting and
future CI adoption noisier than it needs to be.

Fix direction:

- Either fix the current Ruff backlog in one mechanical pass, or narrow the
  configured lint surface to the rules the project intends to enforce today.
- Add a `scripts/check.sh` or `make check` equivalent that runs the exact
  accepted gates.
- Add CI after the command is green. There is currently no `.github/` workflow
  in the repo.

### P2: Packaging metadata says "installable package", but the app is still checkout-bound

`pyproject.toml` defines a package and console script, but the wheel target only
includes `agents`, `storage`, and `tools` (`pyproject.toml:42-64`). Runtime code
reads checkout files directly, including `CLAUDE.md` (`agents/runtime.py:103-109`),
`config/tools.yaml` (`tools/_tools_yaml.py:302-325`), and subagent prompt files
(`tools/_tools_yaml.py:202-208`).

Impact: `uv run hikari-agent` from the repo works, but a built wheel is not a
real deployable artifact. It would likely miss `auth`, `mcp_external`, top-level
`config`, `CLAUDE.md`, `AGENTS.md`, subagent prompts, and YAML config files.

Fix direction:

- Decide whether this is intentionally a repo-local app. If yes, make that
  explicit and avoid pretending the wheel is complete.
- If installation matters, move runtime assets into package data, include all
  importable packages, and use `importlib.resources` instead of repo-root file
  reads.
- Add a packaging smoke test: build wheel, install into a fresh venv, import
  `agents.runtime`, load tool registry, and run `hikari-agent --help` or a dry
  boot.

### P2: The orchestration layer is custom where modern durable runtimes now exist

For a single-user Telegram companion, the current custom orchestration is
reasonable: APScheduler drives periodic jobs (`agents/scheduler.py:19-314`),
SQLite stores state, and Claude Agent SDK sessions provide continuity
(`agents/runtime.py:184-260`, `agents/sdk_pool.py:1-302`).

The place to be careful is long-running or human-in-the-loop workflows. The repo
already implements several "workflow runtime" concerns by hand: single live
SDK client pooling, restart recovery, approval deferral, background task rows,
proactive cadence, reminders, and scheduled reflection. That is not wrong, but
it is now a maintenance surface that modern frameworks explicitly target.

The strongest replacement candidates are not for the main chat loop. They are
for durable multi-step jobs:

- LangGraph: durable execution, human-in-the-loop interrupts, persistence, and
  stateful graph orchestration.
- Pydantic AI durable execution: integrations with Temporal, DBOS, Prefect, and
  Restate while preserving streaming and MCP.
- Google ADK: graph workflows, collaborative agents, sessions, memory, runtime,
  deployment, and observability. It is more attractive if the project moves
  toward Gemini or Google Cloud-managed deployment.

Recommendation: do not rewrite the main agent loop now. Do consider piloting
one bounded workflow, such as calendar heartbeat or reminder sync, behind a
durable runtime before this grows more recovery code.

Sources:

- LangGraph overview: https://docs.langchain.com/oss/python/langgraph
- LangGraph durable execution: https://docs.langchain.com/oss/python/langgraph/durable-execution
- Pydantic AI durable execution: https://pydantic.dev/docs/ai/integrations/durable_execution/overview/
- Google ADK: https://adk.dev/

### P3: Tool semantics are encoded in private YAML, not also in standard MCP annotations

`config/tools.yaml` is useful: it centralizes tool gates, untrusted-output
wrapping, bucket ownership, server definitions, and subagent tool grants. The
registry is validated by `scripts/validate_tool_registry.py`, and that validator
is clean.

The missed modern affordance is MCP/SDK tool annotations. Claude Agent SDK
supports `ToolAnnotations` such as `readOnlyHint`, `destructiveHint`, and
`openWorldHint`; current in-process tool definitions do not appear to use them.
Security should continue to live in the gatekeeper and hooks, but annotations
would give the model and MCP clients better semantic hints.

Fix direction:

- Add annotations to obvious read-only tools: recall, wiki_read, wiki_search,
  codex read/list, weather, currency, arxiv, places read operations.
- Add destructive/write hints where appropriate: reminders create/cancel,
  wiki_append, Apple Notes create, dispatch, decision resolve, day receipt
  writes.
- Keep YAML gates as the security source of truth; treat annotations as
  ergonomics, not authorization.

Source: Claude Agent SDK Python reference, `tool()` and `ToolAnnotations`:
https://platform.claude.com/docs/en/agent-sdk/python

## Modernity Verdict

Overall: modern enough, and more current than most personal-agent repos.

Strong modern choices already present:

- Python 3.12, `uv`, `uv.lock`, Hatch build metadata.
- `claude-agent-sdk` with `ClaudeSDKClient`, hooks, programmatic subagents,
  in-process SDK MCP servers, `can_use_tool`, budget/turn caps, and session
  resume.
- MCP-first tool boundaries, plus external MCP over Streamable HTTP.
- `python-telegram-bot` 22.7 in the lockfile, current async PTB family.
- SQLite WAL, FTS5, `sqlite-vec`, and local-first data ownership.
- Registry-driven tool inventory and untrusted-output wrapping.
- A large regression suite: 1148 passing tests.

Less modern or incomplete:

- Ruff is configured but not green.
- No visible CI.
- No type checker such as Pyright, basedpyright, or mypy.
- No packaging smoke test.
- No dependency-update automation visible.
- External MCP OAuth is close, but not fully aligned with the latest MCP
  resource/audience requirements.

Sources:

- uv project manager: https://docs.astral.sh/uv/
- claude-agent-sdk PyPI, latest 0.2.87 on 2026-05-23:
  https://pypi.org/project/claude-agent-sdk/
- python-telegram-bot PyPI: https://pypi.org/project/python-telegram-bot/
- APScheduler PyPI: https://pypi.org/project/APScheduler/
- sqlite-vec: https://github.com/asg017/sqlite-vec

## Are We Reinventing Something That Already Works?

Short answer: partly, but not in the places I would rip out first.

Do not replace now:

- Main Claude Agent SDK loop. The project is built around Claude Code/Agent SDK
  semantics, subscription quota behavior, hooks, skills, subagents, and MCP
  naming. LangGraph or ADK would add another orchestration layer without a clear
  payoff for the core chat path.
- SQLite personal memory. A local-first bi-temporal memory store is appropriate
  for a single-user companion with privacy-sensitive data. Zep/Graphiti, Letta,
  or Mem0 are worth studying, but replacing the DB today would trade control for
  a new platform dependency.
- Tool registry and prompt-injection wrapping. This is bespoke, but it is
  solving project-specific safety and voice constraints.

Maybe replace or wrap later:

- Long-running scheduled workflows. If calendar/reminder sync, reflection, or
  proactive workflows start needing replay, retries, human edits, and inspection,
  use LangGraph or a Pydantic AI durable execution backend for those workflows.
- Memory extraction. The fact/relation model is getting close to what Zep
  Graphiti and Letta-style memory systems specialize in. Consider borrowing
  patterns or integrating one as an evaluator/reference implementation before a
  migration.
- Observability. The repo logs usage and tool calls, but OpenAI Agents SDK,
  LangSmith/LangGraph, Pydantic Logfire, and ADK all push toward richer traces.
  If failures become harder to explain, use an OpenTelemetry-compatible trace
  path rather than expanding ad hoc logs forever.

## Candidate Tools and Frameworks Checked

### Claude Agent SDK

Best fit for this repo's core because the app is explicitly a Claude/Claude Code
companion runtime. It already uses continuous `ClaudeSDKClient`, hooks,
in-process MCP servers, subagents, and custom tools. Keep.

### LangGraph

Best fit for deterministic, durable, inspectable workflows. It is not obviously
better for Hikari's open-ended chat personality loop, but it is attractive for
approval workflows, background jobs, and multi-step sync tasks if they outgrow
APScheduler plus SQLite rows.

### Pydantic AI

Good modern Python agent framework with MCP support, typed outputs, evals,
Logfire, and durable execution integrations. Strong candidate for isolated
workflow services or structured internal-control calls. A full migration would
lose Claude Agent SDK-specific affordances unless there is a clear provider or
durability reason.

### Google ADK

Modern and broad: Python/TS/Go/Java/Kotlin, graph workflows, collaborative
agents, sessions/memory, deployment, evaluation, and observability. It matters
most if this project shifts toward Gemini/Google Cloud or wants a managed agent
runtime. It is not a reason to rewrite a Claude-Max-based single-user bot today.

### OpenAI Agents SDK

Good reference point for tracing, guardrails, handoffs, and sessions. It is not
a natural primary runtime for this repo unless the model/provider strategy
changes. The main useful import is the product expectation: first-class tracing
should become normal.

### Letta / Zep Graphiti / Mem0

These are the serious "maybe we are reinventing memory" candidates. They offer
agent memory primitives, temporal graphs, and context engineering patterns. The
repo's memory layer is intentionally personal, local, and voice-aware, so I
would not outsource it casually. But I would use these systems as benchmarks:
can Hikari's recall outperform them on this user's actual questions?

Sources:

- Pydantic AI MCP overview: https://pydantic.dev/docs/ai/mcp/overview/
- OpenAI Agents SDK docs: https://openai.github.io/openai-agents-python/
- Letta memory docs: https://docs.letta.com/letta-code/memory
- Zep graph docs: https://help.getzep.com/v2/understanding-the-graph

## Suggested Next Steps

1. Fix the MCP OAuth `resource` gap.
2. Make `uv run ruff check .` green or deliberately narrow it.
3. Add one CI workflow: registry validator, Ruff, tests.
4. Decide packaging stance: repo-local application or installable wheel.
5. Add MCP `ToolAnnotations` to in-process tools.
6. Pilot one durable workflow only if the custom scheduler/recovery layer starts
   hurting. Calendar heartbeat or reminder sync is the right trial size.
7. Add a memory benchmark notebook or test fixture comparing current recall
   against Zep/Letta/Mem0-style retrieval on real anonymized questions.

## Verification Run

Commands run on 2026-05-23:

- `uv run python -m pytest -q`
  - Result: 1148 passed, 18 skipped, 2 warnings in 17.88s.
- `uv run python scripts/validate_tool_registry.py`
  - Result: `validate_tool_registry: clean.`
- `uv run ruff check .`
  - Result: failed with 262 errors.

Note: two `uv run ...` commands needed sandbox escalation because `uv` wanted
access to `/Users/ol/.cache/uv`.
