---
title: Second-Pass Cleanup / Test Debt / CI / Packaging Review
date: 2026-05-24
repo: /Users/ol/agents/hikari-agent
tags: [cleanup, tests, ci, packaging, second-pass]
---

# Second-Pass Cleanup / Test Debt / CI / Packaging Review - 2026-05-24

Scope: current working tree at `/Users/ol/agents/hikari-agent`.

Read as prior context only:

- `codex/dead-code-dead-tests-deep-dive-2026-05-23.md`
- `codex/dead-code-dead-tests-review-2026-05-23.md`
- `codex/2026-05-23-modernity-architecture-review.md`

Working-tree note: those prior report files are deleted in the current working
tree, so I read them from `HEAD` with `git show` only because this review request
explicitly asked for them. I did not restore or edit them.

## 1. Current-State Summary

The cleanup/CI posture is much better than the 2026-05-23 priors. The default
test lane, Ruff, registry validation, generated `.mcp.json` check, lockfile
check, and Layer A/B eval checks all pass on this tree.

Fresh checks run:

- `uv run python -m pytest --collect-only -q`
  - `1641/1659 tests collected (18 deselected)` in 2.11s.
- `uv run python -m pytest -q`
  - `1640 passed, 1 skipped, 18 deselected, 3 warnings` in 43.14s.
- `uv run ruff check .`
  - `All checks passed!`
- `uv run ruff check . --select F401,F841,F811,F821`
  - `All checks passed!`
- `uv run python scripts/validate_tool_registry.py`
  - `validate_tool_registry: clean.`
- `uv run python scripts/regen_mcp_json.py --check`
  - `.mcp.json is up to date.`
- `uv lock --check`
  - `Resolved 131 packages in 3ms.`
- `uv run python scripts/run_evals.py --layer a`
  - `6 pass, 0 fail.`
- `uv run python scripts/run_evals.py --layer b --kind injection,bypass`
  - `18 pass, 0 fail.`
- `uv run python scripts/validate_mcp_servers.py --skip apple_events,apple_shortcuts,google_workspace,notion,github,playwright`
  - `youtube_transcript: OK`; `duckdb` was skipped after `RuntimeError: MCP server did not respond to initialize`; script still exited 0.
- `uv build --wheel --out-dir /private/tmp/hikari-build-second-pass`
  - wheel built, but importing `agents.runtime` from the extracted wheel failed with `FileNotFoundError: Hikari config not found at /private/tmp/hikari-wheel-extract-20260524/config/engagement.yaml`.

Current untracked tests: none. `git ls-files --others --exclude-standard tests`
returned no paths.

Current dirty state is concentrated in `codex/`: many old reports are deleted,
`codex/index.md` is reset for second-pass reviews, and this report is newly
added.

## 2. Findings

### P0

No P0 cleanup, CI, or packaging blocker found.

### P1 - Built wheel is still not a runnable package

`pyproject.toml:49-50` builds a wheel containing only `agents`, `storage`, and
`tools`. The wheel does build, but it is not self-contained:

- `agents/config.py:25-40` resolves `REPO_ROOT` from the installed `agents`
  package and requires `config/engagement.yaml`.
- `tools/_tools_yaml.py:30-31` resolves `config/tools.yaml` from the installed
  `tools` package root.
- `agents/runtime.py:199` reads `CLAUDE.md` from `REPO_ROOT`.
- `agents/runtime.py:100`, `agents/runtime.py:122`, and `agents/runtime.py:138`
  import `auth.*` providers, but `auth` is not included in the wheel.
- `mcp_external` is tested and has launch scripts, but is also not included in
  the wheel.

Verification: after extracting the built wheel to `/private/tmp`, running
`PYTHONPATH=/private/tmp/hikari-wheel-extract-20260524 .venv/bin/python -c "import agents.runtime"`
failed before runtime boot because `config/engagement.yaml` was absent.

Decision needed: either declare this a repo-local launchd app and stop treating
the wheel as a deployable artifact, or make the package real. A real wheel needs
`auth`, `mcp_external`, `config/*.yaml`, `CLAUDE.md`, `.claude/skills`, and any
other runtime assets included through package data or `importlib.resources`.

Before larger feature work continues, add a packaging smoke test: build a wheel
to `/private/tmp`, install or extract it outside the repo, import `agents.runtime`,
load the tool registry, and run the console script with a dry/help path.

### P2 - CI's MCP server validator can pass while a non-skipped server fails

`.github/workflows/ci.yml:44-48` runs `scripts/validate_mcp_servers.py` but sets
`continue-on-error: true`. The script itself also treats every introspection
exception as a soft skip at `scripts/validate_mcp_servers.py:67`, even though
its header says it "Fails closed" for live MCP drift.

The current run demonstrates the problem:

- `youtube_transcript` initialized and validated.
- `duckdb` is configured in `config/tools.yaml:158-170` with
  `mcp-server-motherduck==1.0.6`, but validation printed
  `duckdb: skipped (RuntimeError: MCP server did not respond to initialize)`
  and still exited 0.

That means CI can be green while a non-credential-dependent MCP server is not
actually introspected. Keep explicit `--skip` for credential-bound servers, but
make non-skipped server initialization failures fail the check, or add a
separate `--allow-unreachable` list that is visible in CI.

### P2 - Removed 4h persona drift probes still have schema, retention, and tests

The weekly `drift_canary` path is live, but the old SPASM-style
`persona_drift_probes` feature still looks product-dead:

- `storage/db.py:247-264` creates `persona_drift_probes` and indexes on fresh DBs.
- `storage/db.py:424-430` describes drift canary as independent of the 4h probe
  table, but no 4h writer exists.
- `storage/db.py:2425-2448` keeps read helpers for that table.
- `storage/db.py:2825-2833` keeps a pruner.
- `agents/scheduler.py:217-223` still calls the pruner monthly.
- `config/engagement.yaml:877` still configures `retention.drift_probes_days`.
- `tests/test_pruners.py:65-80`, `tests/test_pruners.py:114-126`, and
  `tests/test_pruners.py:199-204` preserve the stale retention path.
- `agents/runtime.py:685-694` still documents `agents.drift_judge.run_persona_probes`,
  but current grep found no such function.

Delete this whole surface if the weekly canary replaced it. If it is coming
back, restore a real writer/schedule and test that writer. The current tests
only prove manually seeded rows can be pruned.

### P2 - Dead Notion schema cache module still exists

`tools/notion.py` remains fully unreferenced by source, tests, prompts, config,
and README, except for its own contents. Its docstring says to call
`refresh_schema_cache()` at startup (`tools/notion.py:8`), but no such function
exists. The module defines only `get_cached_schema`, `set_cached_schema`, and
`clear_cached_schema` (`tools/notion.py:22-38`).

The Notion subagent uses the external Notion MCP directly, and `config/tools.yaml`
owns tool policy. Delete `tools/notion.py` unless there is an out-of-repo caller.
If caching is wanted, wire it into an actual runtime path and add tests.

### P2 - Daily consolidation still writes data only tests read

Daily reflection writes topic summaries and fact co-occurrence edges:

- `agents/reflection.py:1020` calls `db.fact_relation_insert`.
- `agents/reflection.py:1136` calls `db.episode_summary_insert`.
- `storage/db.py:303-326` creates `episode_summaries` and `fact_relations`.
- `storage/db.py:1705-1745` exposes `episode_summary_insert` and
  `episode_summaries_recent`.
- `storage/db.py:1807-1875` exposes fact relation helpers.

Current reads of `episode_summaries_recent` and `fact_relations_for` are still
test-only (`tests/test_reflection_consolidation.py`,
`tests/test_fact_relations_validity.py`). Meanwhile, Graphiti outbox and
`/memory_diff` are live, and weekly consolidation is wired into
`core_blocks['weekly_consolidation']`.

Either wire these SQLite summary/relation tables into recall, `/memory`, a
debug command, or future-letter style synthesis, or remove the writers and
tests. Keeping write-only memory stores makes later memory work harder to audit.

### P2 - Two tracked skill trees still diverge

Production runtime and project docs point at `.claude/skills`:

- `AGENTS.md:59` says skills live under `.claude/skills`.
- `agents/runtime.py:339-340` uses `setting_sources=["project"]` and `skills="all"`.

But `.agents/skills` is also tracked and divergent:

- `diff -qr .agents/skills .claude/skills` shows differences in
  `character-voice/SKILL.md`, `drive-search/SKILL.md`, and
  `schedule-heartbeat/SKILL.md`.
- `.claude/skills` has `runtime-bridge` and `untrusted-content`; `.agents/skills`
  does not.

If `.agents/skills` is only for the Codex/Hikari development wrapper, keep it
but document that and add a sync/drift check for shared skill names. If not,
delete it and keep `.claude/skills` as the only project skill tree.

### P3 - Small unused helpers remain after Ruff cleanup

Ruff is clean, but plain function-level dead helpers remain:

- `agents/injection_guard.py:154` `extract_urls()` has no caller.
- `tools/voice.py:68` `_max_duration_sec()` is unused, so the
  `voice.max_duration_sec` knob is inert.
- `tools/approvals.py:229` `_safe_args_dump()` has no caller, despite the module
  header saying the audit chain uses it.
- `storage/db.py:2108` `lexicon_prune_stale()` has no caller; active cleanup uses
  `lexicon_decay_and_prune`.
- `storage/db.py:3780` `oauth_token_revoke_family()` has no caller; refresh
  rotation inlines revocation in `oauth_token_consume_refresh`.
- `tools/day_receipt/_db.py:239` `list_dates()`,
  `tools/day_receipt/_render.py:106` `render_summary_table()`, and
  `tools/day_receipt/_shared.py:26` `is_category()` are still unused.

Delete these unless they are intended public helper APIs. If they are intended,
add direct tests and documentation so future cleanup passes do not keep
rediscovering them.

### P3 - `/cost` still presents chat spend that is not instrumented

`README.md:340` advertises `/cost` as "today's spend across Max + OpenRouter
buckets." `agents/telegram_bridge.py:1774-1785` reports background task cost
plus `db.runtime_get("cost_today")`. `tools/budget.py:34-61` reads the same
`cost_today` state and documents read-only behavior.

Current grep found no writer for `cost_today` or `cost_today_date` outside
`tools/budget.py` reads. Background dispatch costs are real; chat costs appear
to stay zero unless an out-of-tree writer exists. Either instrument SDK usage
into this counter, or change the command/docs to say it reports dispatched
OpenRouter/background cost plus a currently untracked chat bucket.

### P3 - A few tests still preserve copied snippets instead of live paths

`tests/test_graph_phase_c.py:181-204` says it simulates the post-init graph boot
path, but it reproduces the intended try/except locally and calls
`graph_mod.get_graph()` directly. The real boot path is in
`agents/telegram_bridge.py:2459-2469`.

Keep the unit test if it is useful, but add a direct test around a factored
`post_init` graph bootstrap helper or around the actual `post_init` path. As
written, the test can pass while the production boot code stops catching graph
startup failures.

### P3 - CI and docs have small determinism/staleness issues

- `.github/workflows/ci.yml:15-18` uses `astral-sh/setup-uv@v3` with
  `version: latest`. Pin a concrete uv version before CI becomes the merge gate.
- `README.md:458` says the hook injects `top-8 retrieved hits`, while
  `agents/hooks.py:5-8` says retrieval now happens by direct recall tool call
  instead of an always-on top-8 injection. Update the README so docs match the
  current memory boundary.

## 3. Previously Reported Issues That Now Look Closed

- CI now exists: `.github/workflows/ci.yml` and `.github/workflows/nightly-evals.yml`.
- Full Ruff is green; the previous unused import/local backlog is gone.
- Default pytest is green: `1640 passed, 1 skipped, 18 deselected`.
- Layer A and Layer B eval CI gates pass locally.
- `scripts/validate_tool_registry.py` is clean.
- `.mcp.json` generation is up to date.
- `uv.lock` is consistent with `pyproject.toml`.
- `agents/reflection.py:522-528` now calls `db.oauth_cleanup_expired()`, closing
  the orphan cleanup helper finding.
- Cadence compatibility shims are gone; `tests/test_phase_j_cleanup.py:43-58`
  asserts `can_send_proactive` and `record_proactive_sent` are absent.
- `voice_critic_log` fresh-schema creation appears gone; only tests mention
  `voice_critic` as historical prompt text.
- The live sycophancy slow test now gates on `CLAUDE_CODE_OAUTH_TOKEN`, matching
  the current SDK auth path.
- MCP `ToolAnnotations` now exist via `tools/_annotations.py`, with tests in
  `tests/test_tool_annotations.py`.
- The old budget write-counter helpers are gone; `tools/budget.py` now documents
  read-only behavior. The remaining issue is the inert chat-cost readout, not
  test-only write counters.

## 4. New Regressions Or Contradictions

- The wheel builds successfully but fails outside the checkout. That is not new
  conceptually, but this pass verified the failure directly.
- CI labels MCP validation as a check, but `continue-on-error: true` plus
  exception-as-skip behavior means current `duckdb` non-response does not fail CI.
- Docs still claim top-8 memory injection even though the hook docstring says
  direct recall replaced that always-on retrieval.
- `/cost` presents a chat cost bucket, but no current source writes the chat cost
  runtime keys.

## 5. Missing Tests / Suggested Verification

Add before larger feature work:

- Packaging smoke: build wheel, install/extract outside repo, import
  `agents.runtime`, load tool registry, and run a dry console-script path.
- MCP validation negative test: a non-skipped server introspection exception must
  fail unless explicitly allowlisted.
- DuckDB MCP live/CI decision: either make it reliably initialize in CI, or move
  it to an explicit `--allow-unreachable duckdb` list with a separate local smoke.
- Fresh-schema cleanup regression for deleting `persona_drift_probes`.
- Skill-tree drift check if both `.agents/skills` and `.claude/skills` stay.
- Actual graph boot degradation test for `agents.telegram_bridge` post-init.
- `/cost` instrumentation test if chat cost should be real.
- Memory-store value test for `episode_summaries` / `fact_relations` if they are
  kept; otherwise deletion tests to ensure the write-only surfaces stay gone.

## 6. Sprint Or Roadmap Implications

Do a small cleanup/CI hardening sprint before broad feature work:

1. Decide packaging stance. If repo-local, document it and remove false
   deployable-wheel expectations. If installable, include runtime assets and add
   the packaging smoke test.
2. Tighten CI MCP validation: remove blanket `continue-on-error`, fail on
   non-skipped server exceptions, and make any allowed unreachable servers
   explicit.
3. Delete the obviously stale surfaces: `tools/notion.py`, old
   `persona_drift_probes` schema/helpers/tests, and the small unused helper
   cluster.
4. Decide memory graph ownership: keep Graphiti outbox plus weekly consolidation,
   but either wire or remove SQLite `episode_summaries` and `fact_relations`.
5. Decide skill-tree ownership and add a sync check if both trees remain.
6. Pin CI's uv setup and keep external MCP packages pinned through
   `config/tools.yaml` plus `scripts/regen_mcp_json.py --check`.

What to keep:

- Current default pytest/Ruff/eval gates.
- `uv.lock`.
- Pinned external MCP package args in `config/tools.yaml`.
- Graphiti outbox durability and `/memory_diff`.
- Weekly consolidation, since it has a live prompt path via core blocks and
  future-letter usage.

What to delete unless deliberately rewired:

- `tools/notion.py`.
- `persona_drift_probes` table/read helpers/pruner/config/tests.
- Unused helper functions listed in P3.
- Write-only `episode_summaries` / `fact_relations` writers if no product path
  is planned.

What to pin:

- CI's uv version (`setup-uv` should stop using `version: latest`).
- Any new bucket-3 MCP package args through the existing pin validator.

What to test before larger feature work:

- Wheel/runtime asset smoke.
- Non-skipped MCP validator failures.
- DuckDB MCP initialization or explicit unreachable policy.
- Skill tree drift if both trees stay.

## 7. Sources Used

Local files and generated artifacts:

- `pyproject.toml`
- `uv.lock`
- `.github/workflows/ci.yml`
- `.github/workflows/nightly-evals.yml`
- `.mcp.json`
- `README.md`
- `AGENTS.md`
- `agents/runtime.py`
- `agents/config.py`
- `agents/hooks.py`
- `agents/reflection.py`
- `agents/scheduler.py`
- `agents/telegram_bridge.py`
- `tools/_tools_yaml.py`
- `tools/_annotations.py`
- `tools/notion.py`
- `tools/budget.py`
- `tools/voice.py`
- `tools/approvals.py`
- `tools/day_receipt/*`
- `storage/db.py`
- `config/tools.yaml`
- `config/engagement.yaml`
- `scripts/validate_mcp_servers.py`
- `scripts/validate_tool_registry.py`
- `scripts/regen_mcp_json.py`
- `tests/test_phase_j_cleanup.py`
- `tests/test_pruners.py`
- `tests/test_graph_phase_c.py`
- `tests/test_tool_annotations.py`
- `tests/persona/test_sycophancy.py`

Commands:

- `git status --short`
- `rg --files`
- `git show HEAD:codex/dead-code-dead-tests-deep-dive-2026-05-23.md`
- `git show HEAD:codex/dead-code-dead-tests-review-2026-05-23.md`
- `git show HEAD:codex/2026-05-23-modernity-architecture-review.md`
- `uv run python -m pytest --collect-only -q`
- `uv run python -m pytest -q`
- `uv run ruff check .`
- `uv run ruff check . --select F401,F841,F811,F821`
- `uv run python scripts/validate_tool_registry.py`
- `uv run python scripts/regen_mcp_json.py --check`
- `uv lock --check`
- `uv run python scripts/run_evals.py --layer a`
- `uv run python scripts/run_evals.py --layer b --kind injection,bypass`
- `uv run python scripts/validate_mcp_servers.py --skip apple_events,apple_shortcuts,google_workspace,notion,github,playwright`
- `uv build --wheel --out-dir /private/tmp/hikari-build-second-pass`
- `python3 -m zipfile -l /private/tmp/hikari-build-second-pass/hikari_agent-0.1.0-py3-none-any.whl`
- `PYTHONPATH=/private/tmp/hikari-wheel-extract-20260524 .venv/bin/python -c "import agents.runtime"`
- `git ls-files --others --exclude-standard tests`
- `diff -qr .agents/skills .claude/skills`

No internet sources were used. I did not need current external API behavior for
this cleanup/CI/packaging pass.
