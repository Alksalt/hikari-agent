# Dead Code / Dead Tests Review - 2026-05-23

Scope: current working tree at `/Users/ol/agents/hikari-agent`.

Notes:
- Existing dirty files at audit start: `agents/proactive.py`, `agents/scheduler.py`, `config/engagement.yaml`, `storage/db.py`, `tests/test_pruners.py`. I did not edit them.
- I treated dynamic SDK tool registration as live when it is reachable via `config/tools.yaml`, `tools._registry`, or `agents.runtime`.
- Ignored cache/worktree material under `.claude/worktrees/` because it is gitignored subagent workspace cache, not source.

## Commands / Checks

- `uv run python -m pytest --collect-only -q`
  - Result: `1166 tests collected in 0.64s`
  - Every `tests/**/test*.py` file contains at least one collected `test_*` function.
- `UV_CACHE_DIR=/private/tmp/hikari-uv-cache uv run ruff check . --select F401,F841,F811,F821`
  - Result: 36 unused import / unused local findings.
- Static import/reference scan over `agents`, `tools`, `storage`, `auth`, `config`, `mcp_external`, `scripts`, and `tests`.
  - 293 Python modules scanned.
  - Excluding explicit entrypoints, dynamic tool manifests, and manual scripts, only one production module had no import/reference path: `tools.notion`.

## Findings

### P1 - `tools/notion.py` looks fully dead

Evidence:
- `tools/notion.py:1` claims to provide a Notion schema introspection cache.
- `tools/notion.py:8` mentions `refresh_schema_cache()`, but no such function exists.
- `tools/notion.py:22`, `tools/notion.py:33`, and `tools/notion.py:37` define cache helpers.
- No production code, tests, prompt files, config, or README references `tools.notion` or any helper in it.
- `agents/subagents/prompts/notion.prompt.md:1` tells the Notion subagent to retrieve schema directly via the external Notion MCP tool, not this cache.

Recommendation:
- Delete `tools/notion.py`, unless there is an out-of-repo caller.
- If the cache is wanted, wire it explicitly and add tests. Right now it is neither a tool nor a reachable helper.

### P1 - Old SPASM `persona_drift_probes` feature appears removed, but schema/pruner/tests remain

Evidence:
- `storage/db.py:243` documents a Phase 11 SPASM probe feature.
- `storage/db.py:250` creates `persona_drift_probes`.
- `storage/db.py:2115` and `storage/db.py:2128` expose read helpers for that table.
- `storage/db.py:2415` exposes `prune_drift_probes_older_than_days`.
- `agents/runtime.py:594` says `run_isolated_turn` is used by `agents.drift_judge.run_persona_probes`, but that function does not exist.
- `agents/drift_canary.py:22` still describes `persona_drift_probes` as an independent 4h feature.
- The only inserts found are in `tests/test_pruners.py:65`; production has no writer or scheduler for the 4h probes.
- `agents/scheduler.py:269` still prunes the table monthly via the retention job.

Dead tests:
- `tests/test_pruners.py:121` and `tests/test_pruners.py:130` test the pruner for a table whose feature has no production writer.
- `tests/test_pruners.py:190` asserts the monthly job calls the drift-probe pruner, preserving the stale retention path.

Recommendation:
- If the SPASM probe feature is intentionally gone, remove:
  - `persona_drift_probes` schema and indexes for fresh DBs.
  - `persona_drift_probe_recent`, `persona_drift_probe_avg`, `prune_drift_probes_older_than_days`.
  - `retention.drift_probes_days`.
  - the monthly prune call and the corresponding tests in `tests/test_pruners.py`.
- If it is coming back, restore a real writer/schedule and add tests around that writer. The current tests only prove manually seeded rows can be deleted.

### P2 - Day Receipt has orphaned standalone-CLI index helpers

Evidence:
- `tools/day_receipt/README.md` lists eight exposed tools: add/today/get/print/week/search/set_note/delete.
- `tools/day_receipt/_db.py:238` defines `list_dates`, but no in-process tool or test calls it.
- `tools/day_receipt/_render.py:106` defines `render_summary_table`, but no in-process tool or test calls it.
- `tools/day_receipt/_shared.py:26` defines `is_category`, while actual validation uses direct `category in CATEGORIES` checks.

Recommendation:
- Either expose a `receipt_list` / `receipt_index` tool and test these helpers, or delete the three helpers from the in-process port.
- If they are kept only for byte-for-byte parity with the standalone CLI, mark them explicitly as intentionally unused so future audits do not rediscover them.

### P2 - Small unused helpers in live modules

Evidence:
- `agents/injection_guard.py:154` defines `extract_urls`; no caller uses it. The active flagging path is `flag_args_with_untrusted_content`, which accepts `recently_seen_untrusted` directly.
- `tools/approvals.py:507` defines `_safe_args_dump`; callers inline `json.dumps(...)` plus `_redact(...)` instead.
- `tools/voice.py:68` defines `_max_duration_sec`; `transcribe_voice` never checks duration, so the config knob is inert.
- `storage/db.py:3159` defines `oauth_token_revoke`; no caller uses it.
- `storage/db.py:3208` defines `oauth_token_revoke_family`; refresh rotation revokes the family inline in `oauth_token_consume_refresh`, so this helper is dead.
- `storage/db.py:1802` defines `lexicon_prune_stale`; active daily cleanup uses `lexicon_decay_and_prune`.

Recommendation:
- Delete pure-unused helpers (`extract_urls`, `_safe_args_dump`, `oauth_token_revoke`, `oauth_token_revoke_family`, `lexicon_prune_stale`) after one final grep.
- For `_max_duration_sec`, either implement the duration check or remove the config/helper. As written, tests do not catch that the duration limit is unused.

### P2 - `.agents/skills` duplicates `.claude/skills` but is not used by this repo runtime

Evidence:
- Runtime uses `setting_sources=["project"]` and `skills="all"` from `agents/runtime.py:248`, which points at project Claude skills.
- README and AGENTS point to `.claude/skills`.
- Tests validate `.claude/skills`, not `.agents/skills`.
- Tracked `.agents/skills` contains overlapping but not identical copies:
  - `.agents/skills/character-voice/SKILL.md` differs from `.claude/skills/character-voice/SKILL.md`.
  - `.agents/skills/drive-search/SKILL.md` differs from `.claude/skills/drive-search/SKILL.md`.
  - `.agents/skills/schedule-heartbeat/SKILL.md` differs from `.claude/skills/schedule-heartbeat/SKILL.md`.
  - `.claude/skills` has `runtime-bridge` and `untrusted-content`; `.agents/skills` does not.

Recommendation:
- Decide which tree is authoritative.
- If `.agents/skills` is only for the Codex/Hikari development wrapper, document that in README/AGENTS and add a sync check.
- If not, delete it. Two divergent persona/skill trees are a drift trap.

### P3 - Live sycophancy test gate is stale for the current SDK auth path

Evidence:
- `tests/persona/test_sycophancy.py:74` says CI/cron supplies `OPENROUTER_API_KEY` or `ANTHROPIC_API_KEY`.
- `tests/persona/test_sycophancy.py:77` skips only when those two env vars are absent.
- The test body calls `agents.runtime.run_isolated_turn` at `tests/persona/test_sycophancy.py:86`, which uses the Claude Agent SDK runtime path, not OpenRouter.
- `tests/test_voice.py:187` correctly gates the live voice test on `CLAUDE_CODE_OAUTH_TOKEN`.
- `tests/persona/test_sycophancy.py:89` imports `judge_outbound` only to assign it to `_` at `tests/persona/test_sycophancy.py:120`.

Recommendation:
- Change the skip condition to match the SDK credential actually needed, likely `CLAUDE_CODE_OAUTH_TOKEN`.
- Remove the unused `judge_outbound` import and comment; if the test wants shared judge construction, factor an actual helper.
- This is not an uncollected dead test, but it is a stale live-test contract that can produce misleading CI behavior.

### P3 - Unused import/local-variable cleanup is available

Ruff found 36 `F401` / `F841` findings. Most are in tests and low-risk to auto-fix.

Production examples:
- `agents/engagement/composer.py:6` unused `typing.Any`.
- `agents/engagement/sender.py:7` unused `typing.Any`.
- `tools/approvals.py:24` unused `httpx`.
- `tools/gatekeeper_can_use_tool.py:43` assigns `reg` and never uses it.

Test examples:
- `tests/test_apple_shortcuts_mcp.py:8` unused `yaml`.
- `tests/test_youtube_transcript_mcp.py:8` unused `yaml`.
- `tests/test_link_shelf.py:274` assigns `real_search` and never uses it.
- `tests/test_proactive_persists_filtered_text.py:70` assigns `fake_bot` and never uses it.

Recommendation:
- Run `UV_CACHE_DIR=/private/tmp/hikari-uv-cache uv run ruff check . --select F401,F841 --fix` for the safe fixes, then review the remaining unsafe `F841` suggestions manually.

## Not Counted As Dead

- `tools/*` feature packages with `ALL_TOOLS`, `PUBLIC_TOOLS`, or `CONFIRMED_TOOLS`: many are dynamically discovered by `tools._registry` or attached by `agents.runtime`, so plain import greps undercount them.
- `scripts/*.py`: treated as manual entrypoints even when not imported.
- Slow/live tests: pytest still collects them; some skip at runtime by env. Only the stale credential gate in `tests/persona/test_sycophancy.py` looked actionable.
- `.claude/worktrees/`: ignored source-wise because `.gitignore` identifies it as Claude Code agent worktree cache. It is about 298M locally, so it is disk cleanup, not dead source.

