---
title: Second-Pass Review — Memory / Graph / Recall Product
date: 2026-05-24
tags:
  - second-pass
  - memory
  - graphiti
  - recall
  - personalization
---

# 1. Current-State Summary

The current memory product is materially more complete than the 2026-05-23 checklist context suggested. SQLite remains the canonical store for facts, episodes, tasks, messages, core blocks, entities, fact relations, message FTS, embeddings, and Graphiti outbox state. `insert_fact()` now writes the canonical fact, FTS row, and `graph_outbox` row in one transaction (`storage/db.py:1242-1299`), and the scheduler drains that outbox every 30 seconds when Graphiti is enabled (`agents/scheduler.py:362-381`).

The Telegram `/memory` surface now exists: recent facts, substring search, fact detail, forget, correct, session search, provenance, and Graphiti-vs-SQLite debug are all implemented in `agents/telegram_bridge.py:1494-1699`, with tests in `tests/test_telegram_memory_cmd.py`. Session search also has a dedicated MCP tool backed by `messages_fts` (`tools/memory/session_search.py:18-49`, `storage/db.py:4004-4086`).

The risky part is now the read-side contract. `recall()` uses Graphiti as the primary path and falls back to SQLite only when Graphiti returns no edges or raises (`tools/memory/recall.py:71-84`). That means any non-empty graph result masks the canonical SQLite retrieval path, even when SQLite has superseded/forgotten a fact or has better provenance. Until graph invalidation, id mapping, and recovery semantics are tightened, Graphiti should be treated as an experimental index, not the product source of truth.

Review context note: during this review the old `codex/*.md` report files were already reset/deleted from the working tree, leaving only `codex/index.md` with the new second-pass template. I did not restore or modify those prior reports.

# 2. Findings

## P0

No P0 issues found in the current memory/graph/recall source pass.

## P1

### P1-1 — Graph recall can surface stale superseded or forgotten facts

SQLite invalidation is implemented, but Graphiti invalidation is not. Facts are added to Graphiti through insert-only outbox payloads (`storage/db.py:1287-1298`, `storage/graph.py:180-218`). Supersession and invalidation paths update SQLite only: `supersede_fact()` stamps the old row and deletes its FTS/vector entries (`storage/db.py:1455-1468`), while `mark_fact_invalid()` stamps SQLite validity and fact-relation edges (`storage/db.py:1496-1539`). Neither writes a graph invalidation/tombstone event.

This matters because `remember()` still resolves conflicts through `db.supersede_fact()` (`tools/memory/remember.py:65-69`), `/memory correct` writes a replacement and calls `mark_fact_invalid()` (`tools/memory/correct_fact.py:12-23`), and `recall()` trusts any non-empty Graphiti result before consulting SQLite (`tools/memory/recall.py:75-141`). A stale graph edge can therefore beat the canonical DB and reintroduce a fact the user explicitly corrected or forgot.

Suggested fix: make SQLite the validity authority on reads. Store/round-trip SQLite fact ids in graph payloads, filter graph hits against `facts.status/valid_to`, and either enqueue tombstone/supersession graph events or merge graph and SQLite candidates with SQLite validity as a hard gate.

### P1-2 — Graph outbox can terminal-fail during optional-backend downtime and then look healthy

The scheduler enables the graph drain by default whenever `GRAPHITI_ENABLED` is not exactly `false` (`agents/scheduler.py:362-365`). But `get_graph()` raises if `OPENROUTER_API_KEY` is missing (`storage/graph.py:56-58`). In that state, `process_outbox()` marks each pending row failed on every drain attempt (`storage/graph.py:203-218`), and `graph_outbox_mark_failed()` flips rows to terminal `failed` after five attempts (`storage/db.py:4203-4213`).

After that, normal recovery tools do not pick them back up: `graph_outbox_pending()` excludes failed rows (`storage/db.py:4179-4185`), `scripts/backfill_graph_outbox.py` only inserts rows for facts with no existing outbox row (`scripts/backfill_graph_outbox.py:35-43`), startup health checks only pending count (`agents/health.py:115-123`), and `/status` also reports pending only (`agents/cockpit.py:329-333`). This contradicts the README’s “Graphiti is optional” recovery story, where disabled Graphiti should let rows accumulate until re-enabled (`README.md:462-466`).

Suggested fix: do not terminal-fail rows for missing credentials or disabled Graphiti. Leave them pending with backoff, or use a retryable status. Health and `/status` should show pending, failed, sent, disabled/enabled, and last error.

### P1-3 — Open-loop task memory is injected always-on without sanitizer or data wrapping

Tasks are part of the memory surface: open tasks are injected every user turn as priority-1 context (`agents/hooks.py:518-521`, `agents/hooks.py:574-588`). But task text is stored raw through `task_create()` and `db.create_task()` (`tools/memory/task_create.py:24-30`, `storage/db.py:1888-1896`), and `_format_open_tasks()` renders subject/description directly under a memory heading (`agents/hooks.py:257-268`).

The risky path is not only direct user task creation. `reflection_after_task()` wraps dispatched task inputs/results as untrusted data for the reflection prompt (`agents/reflection.py:792-820`), then writes model-extracted `open_loops` directly into tasks with no sanitizer (`agents/reflection.py:859-862`). A task like “ignore previous instructions…” can become always-on memory on the next turn.

Suggested fix: sanitize task subject/description with the same instruction-shape guard used for observations/core blocks, and wrap injected task text as remembered data or neutralize forgeable headings/delimiters before rendering.

## P2

### P2-1 — Graph recall lacks fact ids and provenance, breaking correction workflows

The graph recall response emits text, score, valid/invalid timestamps, and then fills attribution/source fields with `None` (`tools/memory/recall.py:99-125`). Legacy SQLite recall includes `kind`, `ref_id`, attribution, source message id, source span hash, and recorded time (`tools/memory/recall.py:187-200`). Meanwhile `mark_fact_invalid` tells the model to invalidate “a stored fact by its numeric id (returned earlier by `recall`)” (`tools/memory/mark_fact_invalid.py:13-21`).

With Graphiti as primary, the usual “recall -> correct/forget by id” loop is not reliable because graph hits do not expose the SQLite fact id. The user can sometimes find the fact via `/memory search`, but the core tool path has lost its correction handle.

Suggested fix: require graph payloads/results to carry canonical `fact_id`, or demote graph hits to expansion/context until they can be linked back to SQLite provenance.

### P2-2 — `GRAPHITI_ENABLED=false` disables the drain, not recall

The only runtime check for `GRAPHITI_ENABLED` is in scheduler job registration (`agents/scheduler.py:362-381`). `recall()` always calls `storage.graph.search()` first (`tools/memory/recall.py:75`), and `storage.graph.search()` always calls `get_graph()` and logs an exception before returning `[]` on failure (`storage/graph.py:223-229`). If Graphiti is intentionally disabled or lacks credentials, each recall can still attempt graph init, log an error, and then fall back.

Suggested fix: make `storage.graph.search()` or `recall()` respect `GRAPHITI_ENABLED=false` with a quiet legacy fallback. Missing credentials should be a debug-level disabled path, not an exception path on every memory question.

### P2-3 — Reflection provenance accepts unverified `source_message_id`

The daily reflection prompt requires facts to cite a message id from the recent transcript (`agents/reflection.py:161-162`), but the writer only coerces the model-provided value to int and stores it (`agents/reflection.py:219-233`, `agents/reflection.py:255-269`). It does not verify that the id exists, is one of the prompt’s recent messages, has the expected role, or contains the cited `source_text`. `fact_provenance()` is a left join, so bogus ids silently become missing source rows (`storage/db.py:1434-1443`).

This weakens `/memory why` and makes provenance look more precise than it really is.

Suggested fix: pass the allowed message-id set from prompt construction to the writer, reject out-of-window ids, and verify `source_span_hash` against actual source content when possible.

## P3

### P3-1 — Config advertises Graphiti LLM fallbacks, but code ignores them

`config/engagement.yaml` documents `graph.llm_fallbacks` as “tried in order if primary errors” (`config/engagement.yaml:919-927`). `storage/graph.py` only reads `graph.llm_model` and constructs a single `OpenAIGenericClient` (`storage/graph.py:63-72`). This is a docs/config contradiction, not an immediate recall bug.

Suggested fix: either implement fallback selection or delete the unused config/comment.

### P3-2 — Fact invalidation helpers have inconsistent history/edge behavior

`mark_fact_invalid()` preserves FTS/vector rows by design and invalidates fact-relation edges (`storage/db.py:1496-1539`). The older `supersede_fact()` helper deletes FTS/vector rows and does not invalidate relations (`storage/db.py:1455-1468`). Current production paths still call the older helper from `remember()` and daily reflection supersession (`tools/memory/remember.py:65-69`, `agents/reflection.py:262-273`).

This is not visible in normal active-fact recall today, but it will matter for historical recall, graph drilldown, and any future “why/related facts” UI.

Suggested fix: route all invalidation through one helper, or make `supersede_fact()` delegate to `mark_fact_invalid()`.

# 3. Previously Reported Issues That Now Look Closed

- Durable Graphiti writes are no longer fire-and-forget. Fact insert now writes `graph_outbox` in the same transaction (`storage/db.py:1287-1298`), the scheduler has a drain job (`agents/scheduler.py:362-381`), and `tests/test_graph_outbox.py` covers schema, insert dedup, backfill, and drain behavior.
- `/memory` is no longer just a design idea. The command suite exists (`agents/telegram_bridge.py:1494-1699`) and is covered by `tests/test_telegram_memory_cmd.py`.
- SQLite-side bi-temporal recall filtering is fixed. `storage.retrieval._hydrate()` drops invalid/superseded facts (`storage/retrieval.py:96-111`), and `tests/test_facts_bitemporal.py` verifies invalidated facts do not leak through legacy retrieval.
- Memory sanitizer hardening is substantially improved for high-priority surfaces. `reflection_sanitize` has an allowlist, length caps, delimiter/tag/control-token patterns (`agents/reflection_sanitize.py:23-94`), core blocks are wrapped in `<remembered>` tags (`agents/hooks.py:171-195`), and reflection sanitizes fact fields, observations, noticings, and peer updates before storing (`agents/reflection.py:207-328`).
- Session scratch no longer appears to be a global default bucket. `session_scratch` requires `session_id` in schema (`storage/db.py:344-356`), callback dedup uses `db.get_session_id()` only when present (`agents/callback_surface.py:94-119`), and the old scratch MCP allowlist is tested as removed (`tests/test_allowlist_completeness.py:78-82`).

# 4. New Regressions Or Contradictions

- README says Graphiti is optional and durable, but default scheduler behavior can turn missing credentials into terminal failed outbox rows; health/status then show only pending rows, not failed rows (`README.md:462-466`, `agents/scheduler.py:362-381`, `agents/health.py:115-123`, `agents/cockpit.py:329-333`).
- Config says Graphiti has an LLM fallback chain, but only the primary model is used (`config/engagement.yaml:919-927`, `storage/graph.py:63-72`).
- The user-facing memory correction loop assumes recall returns fact ids, but Graphiti-primary recall does not (`tools/memory/recall.py:99-125`, `tools/memory/mark_fact_invalid.py:13-21`).
- `codex/index.md` says the old reports were intentionally removed and the second-pass files are expected next; current working tree matches that reset, so prior `codex/*.md` files should not be treated as active state.

# 5. Missing Tests / Suggested Verification

Focused tests run in this review:

```bash
uv run python -m pytest tests/test_graph_outbox.py tests/test_recall_graph_phase_d.py tests/test_facts_bitemporal.py tests/test_fact_relations_validity.py tests/test_telegram_memory_cmd.py tests/test_session_search.py tests/test_memory_sanitizer.py tests/test_entities_and_provenance.py tests/test_recall_provenance.py -q
```

Result: 75 passed, 1 upstream `graphiti_core` Pydantic deprecation warning.

```bash
uv run python -m pytest tests/test_health.py tests/test_inject_memory_cull.py tests/test_reflection_source_delimiters.py tests/test_callback_surface.py tests/test_working_memory_block.py tests/test_inject_memory_entrypoint_aware.py -q
```

Result: 79 passed, 1 upstream `graphiti_core` Pydantic deprecation warning.

Suggested new tests:

- Graph recall stale-hit regression: seed old/new SQLite facts, supersede old, mock Graphiti returning the old edge, assert `recall()` filters it or falls back to SQLite.
- Graph correction-handle regression: graph recall hit must include canonical `fact_id` and provenance when the graph edge came from a SQLite fact.
- Outbox recovery regression: missing `OPENROUTER_API_KEY` or `GRAPHITI_ENABLED=false` must not terminal-fail pending rows; health/status must expose failed rows.
- Task-memory injection regression: `task_create` and `reflection_after_task` should reject or neutralize instruction-shaped task text before `_format_open_tasks()` injects it.
- Reflection provenance regression: model-provided `source_message_id` outside the prompt window or without matching source text should be rejected or stored as unverified.
- Invalidation consistency regression: `supersede_fact()` should invalidate fact relations and follow the same FTS/vector history policy as `mark_fact_invalid()`.

# 6. Sprint Or Roadmap Implications

Treat SQLite as the product truth and Graphiti as experimental until P1-1 and P1-2 are fixed. The `/memory` UX can keep expanding on SQLite-backed facts/session search now, but Graphiti should not be the primary answer path until it carries canonical ids, respects invalidations, and has recoverable outbox semantics.

The highest-leverage sprint slice is: graph validity gate, graph outbox recovery/visibility, then recall id/provenance parity. The task-memory sanitizer can ride in the same memory-hardening sprint because it protects always-on context, not just operator polish.

# 7. Sources Used

Local source/config/docs/tests only. No internet sources were needed.

- `storage/db.py`
- `storage/graph.py`
- `storage/retrieval.py`
- `tools/memory/recall.py`
- `tools/memory/remember.py`
- `tools/memory/correct_fact.py`
- `tools/memory/mark_fact_invalid.py`
- `tools/memory/task_create.py`
- `tools/memory/session_search.py`
- `agents/scheduler.py`
- `agents/hooks.py`
- `agents/health.py`
- `agents/cockpit.py`
- `agents/reflection.py`
- `agents/reflection_sanitize.py`
- `agents/telegram_bridge.py`
- `config/engagement.yaml`
- `config/tools.yaml`
- `README.md`
- `pyproject.toml`
- `tests/test_graph_outbox.py`
- `tests/test_recall_graph_phase_d.py`
- `tests/test_facts_bitemporal.py`
- `tests/test_fact_relations_validity.py`
- `tests/test_telegram_memory_cmd.py`
- `tests/test_session_search.py`
- `tests/test_memory_sanitizer.py`
- `tests/test_entities_and_provenance.py`
- `tests/test_recall_provenance.py`
- `tests/test_health.py`
- `tests/test_inject_memory_cull.py`
- `tests/test_reflection_source_delimiters.py`
- `tests/test_callback_surface.py`
- `tests/test_working_memory_block.py`
- `tests/test_inject_memory_entrypoint_aware.py`
