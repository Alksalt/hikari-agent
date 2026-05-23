# Dead Code / Dead Tests Deep Dive - 2026-05-23

Scope: current working tree at `/Users/ol/agents/hikari-agent`.

This is a companion to `codex/dead-code-dead-tests-review-2026-05-23.md`. It focuses on deeper stale surfaces: code that is technically imported but product-dead, write-only tables, compatibility shims kept alive by tests, and tests that simulate old contracts instead of exercising the live path.

Workspace note:
- Active uncommitted changes existed during this audit: `agents/reflection.py`, `agents/telegram_bridge.py`, `pyproject.toml`, `tools/memory/remember.py`, `uv.lock`, `storage/graph.py`, `tests/test_graph_phase_c.py`, plus unrelated untracked `codex/*.md` reports.
- I did not modify source or tests. I only added this report.

## Checks

- `uv run python -m pytest --collect-only -q`
  - Result: `1177 tests collected in 2.24s`
  - Warning: `graphiti_core` emits a Pydantic v2 deprecation warning during collection.
- `uv run python scripts/validate_tool_registry.py`
  - Result: `validate_tool_registry: clean.`
- `uv run ruff check . --select F401,F841,F811,F821`
  - Result: 39 unused import / unused local findings.
- Static import/reference scan over `agents`, `tools`, `storage`, `auth`, `config`, `mcp_external`, `scripts`, and `tests`.
  - `tools.notion` remains the only fully unreferenced production module from the first report.
  - `auth.google` is not dead: `config/scopes.yaml` references it dynamically by provider class.
  - `tools.link_shelf.handlers` is not dead: `tools.link_shelf.__init__` references it through lazy tool implementation strings.

## Findings

### P1 - Cadence compatibility shims are explicitly marked for deletion, but tests still depend on them

Evidence:
- `agents/cadence.py:163` defines `proactive_count_last_7d`.
- `agents/cadence.py:173` defines `can_send_proactive`.
- `agents/cadence.py:183` defines `record_proactive_sent`.
- `agents/cadence.py:168` says these are "compat shims (deleted in Phase F, Sprint 2)".
- `agents/cadence.py:169` says "DO NOT call from new code. Use can_send(source, pool) directly."
- Static references show no production callers for these three helpers.
- Tests still call or monkeypatch the old shims:
  - `tests/test_proactive_intel.py:89`
  - `tests/test_daily_checkin_cadence.py:43`
  - `tests/test_proactive_sdk_error_guard.py:54`
  - `tests/test_visible_proactive_is_recorded.py:39`
  - `tests/test_proactive_persists_filtered_text.py:98`
  - `tests/test_cadence_governor.py:187`

Why it matters:
- The tests preserve an API the source itself says should be deleted.
- Some proactive tests can remain green while still exercising the compatibility layer instead of the current pool-aware governor contract.

Recommendation:
- Update tests to call `cadence.can_send(source, pool)` and the pool-specific recorders:
  - `record_spontaneous_sent`
  - `record_ceremony_sent`
  - `record_user_anchored_sent`
- Then delete `proactive_count_last_7d`, `can_send_proactive`, and `record_proactive_sent`.

### P1 - `voice_critic_log` is dead schema from a removed outbound-critic feature

Evidence:
- `storage/db.py:368` still documents "voice_critic Haiku verdicts on outbound drafts".
- `storage/db.py:371` creates `voice_critic_log`.
- `storage/db.py:380` creates `idx_voice_critic_log_created`.
- No writer or reader references `voice_critic_log`.
- Current outbound hardening uses `agents/post_filter.py`:
  - `agents/telegram_bridge.py:43` imports `post_filter`.
  - `agents/telegram_bridge.py:230` calls `post_filter.rewrite_or_fallback`.
  - `agents/post_filter.py:426` implements the active rewrite/fallback strategy.
- `tests/test_smoke.py:241` notes that Stream D removed `voice_critic`.

Why it matters:
- New databases still create a table and index for a feature that appears to have been superseded.
- This makes schema audits noisier and implies telemetry exists when it does not.

Recommendation:
- Stop creating `voice_critic_log` for fresh DBs unless an out-of-repo reader depends on it.
- If old local DBs may contain historical rows, leave migration cleanup separate from fresh schema cleanup.

### P1 - SQLite consolidation summaries and relation edges are write-only in production

Evidence:
- Production writes episode summaries:
  - `agents/reflection.py:948` calls `db.episode_summary_insert`.
  - `storage/db.py:1396` defines `episode_summary_insert`.
- The only read helper is test-only:
  - `storage/db.py:1426` defines `episode_summaries_recent`.
  - References to `episode_summaries_recent` are in `tests/test_reflection_consolidation.py`.
- Production writes fact relation edges:
  - `agents/reflection.py:832` calls `db.fact_relation_insert`.
  - `storage/db.py:1498` defines `fact_relation_insert`.
  - `storage/db.py:1228` invalidates relation rows when a fact is superseded.
- The read helper is test-only:
  - `storage/db.py:1542` defines `fact_relations_for`.
  - References to `fact_relations_for` are in `tests/test_reflection_consolidation.py` and `tests/test_fact_relations_validity.py`.
- New graph work now adds a second memory graph surface:
  - `storage/graph.py:63` defines `add_episode_safe`.
  - `storage/graph.py:94` defines `search`.
  - `agents/telegram_bridge.py:1369` adds `/memory_diff` to compare SQLite retrieval vs Graphiti.
  - `agents/telegram_bridge.py:1750` registers `/memory_diff`.

Why it matters:
- The SQLite summary and relation tables are not dead at the write path, but their production value is currently dead: rows are created and maintained without a retrieval, prompt, debug, or operator surface consuming them.
- The new Graphiti/Kuzu dual-write path makes this more urgent because there are now two graph-ish memory stores: one visible through `/memory_diff`, and one silently accumulating relation/summary rows.

Recommendation:
- Decide which memory graph is authoritative.
- If SQLite summaries/relations are still wanted, wire them into retrieval, a prompt block, or an operator/debug command.
- If Graphiti supersedes them, remove the SQLite writers/read helpers and replace the tests with Graphiti migration or parity tests.

### P2 - OAuth cleanup is tested as a daily-reflection job, but no caller exists

Evidence:
- `storage/db.py:3232` defines `oauth_cleanup_expired`.
- Its docstring says it is "Called from daily reflection."
- `tests/test_mcp_external_oauth.py:433` repeats that contract: "oauth_cleanup_expired is meant to be called from the daily reflection".
- `agents/reflection.py:232` runs several daily cleanup steps, but not OAuth cleanup.
- `agents/scheduler.py:274` prunes `oauth_audit_log`, but not expired OAuth codes/tokens.
- Static search found no production caller for `oauth_cleanup_expired`.

Why it matters:
- Expired authorization codes and expired/revoked tokens can accumulate forever.
- The test proves the helper works when manually called, not that the retention behavior promised by the docstring actually happens.

Recommendation:
- Wire `db.oauth_cleanup_expired()` into daily reflection or monthly retention.
- If cleanup is intentionally manual, update the docstring and test name/body so the test stops asserting a nonexistent schedule contract.

### P2 - Budget write counters are test-only, while `/cost` reads the raw runtime state

Evidence:
- `tools/budget.py:47` defines `record_tool_call`.
- `tools/budget.py:65` defines `calls_in_window`.
- `tools/budget.py:79` defines `record_cost`.
- Static references show these write helpers are only called from `tests/test_smoke.py:325` and `tests/test_smoke.py:338`.
- `agents/telegram_bridge.py:1425` reads `db.runtime_get("cost_today")` directly for `/cost`, instead of using `tools.budget.cost_today`.
- `tools/budget.py:10` says `daily_cap_exceeded()` exists for future enforcement and is not called anywhere.

Why it matters:
- The budget module contains a tested counter path that runtime does not update.
- `/cost` can look like a live chat-cost readout while chat cost remains at zero unless something else writes `cost_today`.

Recommendation:
- Either instrument the live SDK/tool dispatch path to call `record_tool_call` and `record_cost`, or shrink `tools/budget.py` to the actually-live readout behavior.
- If this is intentionally deferred, mark the tests as future-contract tests so they are not mistaken for live coverage.

### P2 - Drift and decision analytics readbacks are test-only surfaces

Evidence:
- `storage/db.py:2036` defines `drift_canary_recent`.
- `storage/db.py:2049` defines `drift_canary_recent_by_probe`.
- Static references for both are tests only, mainly `tests/test_drift_canary.py`.
- `storage/db.py:3391` defines `decision_brier_score`.
- Static references for `decision_brier_score` are tests only:
  - `tests/test_decision_log.py:36`
  - `tests/test_decision_log.py:68`
  - `tests/test_decision_log_brier_e2e.py:38`

Why it matters:
- These are plausible operator/report helpers, but there is no command, scheduled report, or reflection read path for them.
- The tests preserve analytical helper APIs without proving anyone can see or use the analytics.

Recommendation:
- If these metrics are useful, expose them through an owner command, daily/weekly report, or `codex` report generator.
- Otherwise move them to test helpers or delete the readbacks and keep only the write path needed by live telemetry.

### P2 - The new Graphiti boot-failure test copies the snippet instead of exercising the boot path

Evidence:
- `tests/test_graph_phase_c.py:165` says it "Simulates the post_init boot code path with get_graph raising."
- `tests/test_graph_phase_c.py:184` then reproduces the intended try/except snippet locally.
- The test patches `graph_mod.get_graph` and calls `graph_mod.get_graph` directly.
- It does not call the actual Telegram boot/post-init path that would own the degradation behavior.
- `tests/test_graph_phase_c.py:3` says all Graphiti tests mock Graphiti and avoid the real API/filesystem path.

Why it matters:
- This is a shallow unit test, not a boot-path regression test.
- It can pass even if the real boot path stops initializing the graph, stops catching graph startup failures, or logs a different degradation path.

Recommendation:
- Keep the storage-level mocked tests for fast unit coverage.
- Add one test around the actual Telegram app boot/post-init hook, or factor the graph boot into a small function and test that function directly.
- Remove the unused `logging` import at `tests/test_graph_phase_c.py:169`.

### P3 - Unused import/local cleanup grew to 39 findings

Ruff currently reports 39 `F401` / `F841` findings.

New or notable examples beyond the first report:
- `tests/test_graph_phase_c.py:169` unused `logging`.
- `tests/test_inject_memory_cull.py:16` unused `textwrap`.
- `tests/test_inject_memory_cull.py:18` unused `patch`.
- `tests/test_reflection_consolidation.py:230` assigns `sid` and never uses it.

Production examples remain:
- `agents/engagement/composer.py:6` unused `typing.Any`.
- `agents/engagement/sender.py:7` unused `typing.Any`.
- `tools/approvals.py:24` unused `httpx`.
- `tools/gatekeeper_can_use_tool.py:43` assigns `reg` and never uses it.

Recommendation:
- Run `uv run ruff check . --select F401,F841 --fix`, then review the remaining unsafe unused-local findings manually.

## Revised False Positives

These looked dead under a plain import graph but should not be treated as dead:

- `auth/google.py`
  - Loaded dynamically through `config/scopes.yaml` provider metadata.
- `tools/link_shelf/handlers.py`
  - Loaded dynamically through lazy tool implementation strings in `tools/link_shelf/__init__.py`.
- `storage/db.py:2759` `ids_without_embedding`
  - Used by `scripts/backfill_embeddings.py`.
- `storage/db.py:2780` `bulk_insert_facts`
  - Used by `scripts/migrate_from_current.py`.
- `storage/db.py:2803` `bulk_insert_episodes`
  - Used by `scripts/migrate_from_current.py`.
- `tools/embeddings.py:54` `aembed_batch`
  - Used by `scripts/backfill_embeddings.py`.

## Suggested Cleanup Order

1. Remove or migrate the cadence compatibility shims and update tests away from `can_send_proactive`.
2. Decide the memory graph owner: Graphiti/Kuzu vs SQLite `fact_relations`/`episode_summaries`.
3. Wire or delete `oauth_cleanup_expired`.
4. Remove `voice_critic_log` from fresh schema if no external history reader exists.
5. Decide whether budget counters are live instrumentation or future-contract tests.
6. Run the safe ruff cleanup for unused imports.
