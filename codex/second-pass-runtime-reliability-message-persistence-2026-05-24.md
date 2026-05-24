---
title: Second-Pass Runtime / Reliability / Message Persistence Review
date: 2026-05-24
tags:
  - codex
  - second-pass
  - runtime
  - reliability
  - session
  - proactive
  - persistence
---

# Second-Pass Runtime / Reliability / Message Persistence Review

Scope: current working tree at `/Users/ol/agents/hikari-agent`, inspected as-is. I did not edit production code or tests. Existing and deleted `codex/*.md` files were treated as prior context/checklists only; findings below are based on current source, tests, config, docs, and verification commands.

## 1. Current-state summary

The core runtime split is now real and mostly coherent:

- `run_user_turn()` and `run_visible_proactive()` resume the live SDK session under `_RUN_LOCK`.
- `run_internal_control()` is stateless (`resume=None`, `log_session_id=False`, memory hook disabled) and does not append to `messages`.
- Main chat sends now filter first, send through `send_and_persist()`, then append final delivered text to `messages` after Telegram confirms delivery.
- The proactive scheduler path receives a `send_text()` closure that delegates to `send_and_persist()`, so visible scheduled messages are no longer purely ephemeral.
- `proactive_gate.reserve_and_send()` gives proactive producers a global reservation/final gate.
- `media_outbox` exists and records text/photo/sticker/document attempts, while the photo outbox has an actual drain/retry loop.

Verification snapshot:

- `uv run python -m pytest -q` -> `1640 passed, 1 skipped, 18 deselected, 3 warnings`.
- Focused runtime suite -> `101 passed` across send/persist, session isolation, SDK pool, media outbox, proactive gate, health, reaction/start rows.
- `uv run python -m compileall agents storage tools` -> passed.
- `UV_CACHE_DIR=/private/tmp/hikari-uv-cache uv run python scripts/validate_tool_registry.py` -> `validate_tool_registry: clean.`
- Initial registry validation without `UV_CACHE_DIR` failed because the sandbox could not open `/Users/ol/.cache/uv`; rerunning with a writable cache succeeded.

No P0 was found in the current tree. The remaining serious risks are around session ownership after content-block/document turns, live SDK client recycling, and whether the outbound ledger is an audit trail or a real durable outbox.

## 2. Findings, ordered P0/P1/P2/P3

### P0

No current P0 found in this pass.

### P1 - Content-block/document turns still have an unresolved live-session ownership risk

Current document ingest records a compact event row, then calls `run_user_turn_blocks(prompt_blocks)` for PDF/image/text block input (`agents/telegram_bridge.py:1175-1199`). `run_user_turn_blocks()` intentionally avoids the persistent live client and calls `_invoke_sdk(..., resume=db.get_session_id(), log_session_id=False)` through an ephemeral SDK client (`agents/runtime.py:584-606`). The success-path test explicitly pins that it must not update the stored session id (`tests/test_proactive_session_isolation.py:130-156`).

That fixes one version of the older multimodal fork bug (the ephemeral content-block client no longer overwrites the persistent live pointer), but it leaves the ownership question open: after a document turn succeeds, neither the stored `session_id` nor the already-connected persistent live client is advanced/reconnected to a session that definitely includes the document turn. The next normal text/proactive turn uses the persistent live client path (`agents/runtime.py:354-435`, `agents/runtime.py:561-581`), not the content-block client.

Impact: follow-up questions after a document can silently lose the document turn in the Claude SDK conversation state unless the SDK guarantees cross-client live synchronization for resumed sessions. The local code does not prove or enforce that guarantee. SQLite has the compact event row and assistant reply, but the hook does not inject recent transcript by default; the live SDK session remains the main continuity channel.

Recommended fix: make content-block turns first-class in the live-session lifecycle. Either route blocks through a persistent-safe live path, or after successful block turns capture/advance to the returned session and reconnect/replace the live client before the next turn. Add a regression test with fake live and block clients: document turn -> follow-up text turn must see the document turn or force a documented reconnect.

### P1 - Live SDK client recycle can race the next turn

`_invoke_sdk_persistent_live()` schedules `_maybe_schedule_live_recycle()` before returning (`agents/runtime.py:434`). Because `run_user_turn()` / `run_visible_proactive()` are still inside `_RUN_LOCK` until `_invoke_sdk()` returns, this is not "outside `_RUN_LOCK`" in practice (`agents/runtime.py:572-581`, `agents/runtime.py:619-628`).

The pool code itself says recycle must happen outside `_RUN_LOCK` so it happens at the next idle window (`agents/sdk_pool.py:282-287`), but `_maybe_schedule_live_recycle()` immediately creates an async task that calls `_reconnect_live()` (`agents/sdk_pool.py:303-310`). `_reconnect_live()` uses only the pool `connect_lock`; it does not acquire the live turn `_RUN_LOCK` (`agents/sdk_pool.py:219-237`).

Impact: at the recycle threshold, the reconnect task can disconnect/replace the live client while a new user/proactive turn is queued or just starting. The current tests prove the threshold triggers reconnect (`tests/test_sdk_pool.py:219-244`), but they do not prove it waits for an idle live-session window.

Recommended fix: move live-client recycling under the same ownership as `_RUN_LOCK` or make the recycle operation a pending flag consumed by the next turn before it calls `client.query()`. Add a test with threshold `1`, a delayed second turn, and assertions that disconnect does not occur while `receive_response()` is active.

### P1 - `media_outbox` is not yet a durable outbound text queue

`send_and_persist()` inserts a `media_outbox` row before every text/photo send and labels that as "crash-safe durability" (`agents/messaging.py:111-131`). It then sends Telegram (`agents/messaging.py:142-163`), marks the outbox row sent (`agents/messaging.py:167-172`), and only after that appends the final text to `messages` (`agents/messaging.py:174-184`).

The only actual drainer in the bridge is `_drain_photo_outbox()`, which queries `media_outbox_pending(kind="photo")` and sends those files (`agents/telegram_bridge.py:197-245`). There is no corresponding text/sticker/document drainer. Tests confirm a failed text send leaves a failed/pending outbox row (`tests/test_send_and_persist_api.py:367-386`), but no test or worker demonstrates that a restart later replays or reconciles that text row. Non-photo rows also become terminal on first failure in `media_outbox_mark_failed()`.

Impact: the table is useful as an audit/health signal, but it does not yet satisfy the roadmap/runbook promise of a durable outbound ledger for visible text. If the process exits after the pre-send insert but before Telegram send, the pending text row is visible but not replayed. If it exits after Telegram send but before `messages` append, the row may be marked sent while `messages` still lacks the visible assistant text.

Recommended fix: choose and document the contract. If `media_outbox` is audit-only for text, rename comments/health checks accordingly. If it is meant to be durable, add a generic outbox worker with idempotent Telegram send semantics, source-specific payloads, and reconciliation for the sent-before-DB-append window.

### P2 - Several visible Telegram replies still bypass `send_and_persist()`

The main happy paths are centralized, but many visible replies still call Telegram directly:

- Politeness refusals in text/photo/voice/document paths use `message.reply_text(...)` (`agents/telegram_bridge.py:467-474`, `agents/telegram_bridge.py:560-570`, `agents/telegram_bridge.py:693-703`, `agents/telegram_bridge.py:1131-1142`).
- Runtime/error fallbacks use direct `reply_text()` (`agents/telegram_bridge.py:511-516`, `agents/telegram_bridge.py:615-619`, `agents/telegram_bridge.py:730-734`, `agents/telegram_bridge.py:1187-1193`, `agents/telegram_bridge.py:1227-1230`).
- Voice transcription failure, document too-large, fake-PDF refusal, and location ack are direct sends (`agents/telegram_bridge.py:674-690`, `agents/telegram_bridge.py:1100-1106`, `agents/telegram_bridge.py:1148-1155`, `agents/telegram_bridge.py:830`).
- `/silence` and `/unsilence` direct-send their acks (`agents/telegram_bridge.py:1236-1269`).
- Background task listener and legacy approval prompts direct-call `bot.send_message()` (`agents/background_listener.py:150-170`, `tools/approvals.py:80-90`).

Some of these may intentionally be operator/system acks rather than relationship memory. The contradiction is that `AGENTS.md` states the runtime split enforces "final-sent text is what gets persisted" (`AGENTS.md:13-19`), but the code has no explicit `source='system_ack'`/`persist=False` policy boundary for these direct sends.

Impact: the user can see a real message that is absent from `messages`, handoff/reflection, feedback joins, and media outbox health. That is mostly acceptable for throwaway command output, but less acceptable for rude-turn refusals and LLM failure fallbacks because the next turn's state may not reflect what the user just saw.

Recommended fix: inventory every direct Telegram send and route through `send_and_persist()` or a deliberately named `send_ephemeral_ack()` wrapper that records why it is excluded.

### P2 - Observation/noticing surfacing is now delivery-gated, but not mention-gated

The old "mark surfaced during hook injection" problem is partially fixed: `_format_observations()` and `_format_noticings()` stash pending ids in `runtime_state` (`agents/hooks.py:314-380`, `agents/hooks.py:387-431`), and `postsend.mark_pending_surfaced()` drains those ids only after successful send/DB append (`agents/postsend.py:64-80`).

However, `mark_pending_surfaced()` marks every injected id as surfaced, regardless of whether the final text actually mentioned the observation/noticing. The module docstring acknowledges the original failure cases included "the model might not mention the observation" and "the post_filter could rewrite it out" (`agents/postsend.py:3-8`), but the current implementation only protects send failure and culling, not actual semantic surfacing.

Impact: observations can still be consumed without the user seeing them; it just happens after a successful unrelated reply instead of at hook time. This is a reliability issue for the noticing/persona loop, not a delivery failure.

Recommended fix: either rename the state to `injected` and track separate `surfaced_at`, or require a marker/reference check before marking surfaced. A practical middle ground is "retry up to N injections unless the final sent text contains a normalized snippet/key."

### P2 - Content-block ProcessError path can still clear the stored session

`run_user_turn_blocks()` passes `retry_on_process_error=True` (`agents/runtime.py:598-606`). In `_invoke_sdk()`, any first-attempt `ProcessError` with a non-empty `session_id` clears `db.set_session_id("")` before retrying fresh (`agents/runtime.py:542-555`). That behavior is correct for normal user turns with a suspect stored session, but it conflicts with the content-block success contract that this path must not mutate the persistent live session pointer (`agents/runtime.py:590-596`, `tests/test_proactive_session_isolation.py:130-156`).

Impact: a failed document turn can clear the stored session id even though the persistent live client may still be healthy and connected. On a future reconnect, the live client would restart without resume context.

Recommended fix: make `run_user_turn_blocks()` error policy explicit. If the goal is "never mutate live pointer," disable session clearing for this path and surface the document failure. If the goal is "stale session self-heal," reconnect/replace the live client as part of the same recovery so DB and pool do not diverge.

### P3 - Message source taxonomy is coarser than the scheduler/product taxonomy

The bridge's scheduler `send_text()` closure says it covers "heartbeat, daily_checkin, morning_brief, decision_log, etc." but always persists `source="proactive"` into `messages` (`agents/telegram_bridge.py:2338-2361`). `proactive_events` still carries producer-level source for gated proactive sends, but the `messages` table loses that distinction. The manual daily-checkin pre-router can use `_send_text_with_choreography(..., source="daily_checkin")` (`agents/telegram_bridge.py:439-451`), so the inconsistency depends on entrypoint.

Impact: handoff/reflection/analytics can tell "proactive" but not which ritual or scheduler produced the assistant row without joining to another table. This is not a correctness failure today, but it will matter for cadence learning and audit views.

Recommended fix: thread an optional `source` through the scheduler `send_text` contract or consistently use `proactive_events` as the source-of-truth and document `messages.source='proactive'` as intentionally coarse.

### P3 - Proactive persistence regression coverage is thinner than the comments imply

`tests/test_visible_proactive_is_recorded.py` is now a comment-only placeholder saying coverage moved elsewhere (`tests/test_visible_proactive_is_recorded.py:1-5`). `tests/test_proactive_persists_filtered_text.py` now tests tuple unpacking rather than the bridge scheduler `send_text()` closure or an end-to-end scheduled send (`tests/test_proactive_persists_filtered_text.py:1-12`, `tests/test_proactive_persists_filtered_text.py:54-74`).

There is good adjacent coverage for `send_and_persist()`, ceremony `telegram_message_id` propagation, proactive reservations, and engagement sender. Still, the exact regression "scheduler send_text persists final filtered text in messages" is mostly covered by composition of helpers rather than a direct integration test.

Recommended fix: add one small bridge-level test around the `post_init` `send_text` closure or factor that closure into a testable module function.

## 3. Previously reported issues that now look closed

- Pre-filter draft persisted instead of final sent text: closed for main chat path. `_send_with_choreography()` filters/rewrite first, delegates to `send_and_persist()`, and appends only after Telegram success (`agents/telegram_bridge.py:323-353`, `agents/messaging.py:174-184`). Regression tests pass (`tests/test_final_sent_text_is_persisted.py:114-154`).
- Phantom assistant row on Telegram send failure: closed for `send_and_persist()` callers. Failed sends return `ok=False` and do not append assistant rows (`agents/messaging.py:156-184`); focused and full tests pass.
- Hidden internal-control prompts mutating live SDK session: closed for `run_internal_control()`. It uses `resume=None`, `log_session_id=False`, and `inject_memory_enabled=False` (`agents/runtime.py:631-660`), with passing tests in `tests/test_proactive_session_isolation.py` and `tests/test_internal_prompts_not_logged.py`.
- Synthetic event prompts for `/start`, reactions, photo, voice, and documents: mostly closed. The current code writes compact event rows and sends synthetic prompts through runtime without appending those prompt strings as user messages (`agents/telegram_bridge.py:599-615`, `agents/telegram_bridge.py:717-730`, `agents/telegram_bridge.py:1175-1189`, `agents/telegram_bridge.py:1211-1232`, `agents/telegram_bridge.py:2217-2233`).
- User-visible proactive messages not recorded: mostly closed for the current scheduler closure and engagement sender path. `post_init.send_text()` uses `send_and_persist(..., persist=True)` (`agents/telegram_bridge.py:2338-2361`), and proactive event `telegram_message_id` propagation is tested for ceremonies.
- Proactive collisions: closed at the central gate layer. `reserve_and_send()` serializes sends under `_PROACTIVE_LOCK` and writes terminal sent/aborted state (`agents/proactive_gate.py:52-119`); `tests/test_proactive_global_reservation.py` passes.
- Observation/noticing consumed before delivery: partially closed. It is no longer consumed at injection time, and send failure no longer commits surfaced state. It is still not proof-of-mention gated (see P2).
- Typed calendar/reminder sync replacing prompt-mediated plumbing: current source shows typed adapters/tests for calendar and Apple/GCal reminder sync. This is outside this report's deepest scope, but the prior prompt-plumbing concern no longer appears open in those paths.

## 4. New regressions or contradictions

- `sdk_pool._maybe_schedule_live_recycle()` documents that it must run outside `_RUN_LOCK`, while current runtime calls it before the `_RUN_LOCK`-protected entrypoint returns (`agents/runtime.py:434`, `agents/sdk_pool.py:282-287`).
- `send_and_persist()` calls the pre-send outbox insert "crash-safe durability," but current code only drains pending photo rows; text/sticker/document rows are not replayed (`agents/messaging.py:117-131`, `agents/telegram_bridge.py:197-204`).
- `run_user_turn_blocks()` is documented/tested as not mutating the persistent live pointer on success, but the shared ProcessError retry path can still clear the stored session id (`agents/runtime.py:542-555`, `agents/runtime.py:598-606`).
- `AGENTS.md` states "final-sent text is what gets persisted," but several visible direct-send paths bypass persistence entirely (`AGENTS.md:13-19`, examples in P2 above).
- Current working tree has prior 2026-05-23 `codex/*.md` files deleted and `codex/index.md` rewritten. I treated that as current user/worktree state, not something to revert.

## 5. Missing tests / suggested verification

- Add a block-turn continuity test: fake persistent live client + fake ephemeral block client; after `handle_document()`/`run_user_turn_blocks()`, the next `run_user_turn()` must either see the block turn or prove a reconnect happened.
- Add SDK recycle idle-window test: threshold `1`, one active `receive_response()` and one queued turn; assert `_disconnect()` cannot run while the live client is active.
- Add text outbox recovery drill/test: simulate process exit after `media_outbox_insert()` but before Telegram send, then restart and prove expected replay/abort behavior.
- Add sent-before-DB-append reconciliation test: simulate Telegram returning `message_id`, then DB append raising; verify there is a durable degraded record that future health/status surfaces.
- Add direct-send inventory test: fail if production bridge code calls `reply_text()`/`send_message()` outside approved wrappers, or require every bypass to use `send_ephemeral_ack(reason=...)`.
- Add observation/noticing proof-of-mention tests: injected-but-not-mentioned should remain eligible or move to `injected_at`, not `surfaced_at`.
- Add a bridge-level proactive `send_text()` integration test that proves final filtered scheduler text lands in `messages` with `telegram_message_id`.

## 6. Sprint or roadmap implications

The sprint 7 durability work materially improved the system: tests are green, migration/tool registry health is good, runtime split is real, proactive reservations exist, and the old P0 persistence bug is closed.

The next reliability sprint should avoid adding new user-facing capabilities until these edges are settled:

1. Decide content-block session ownership and fix document-turn continuity.
2. Make the outbound ledger either truly durable for text/sticker/document or explicitly audit-only.
3. Put SDK live-client recycle under the same live-session ownership as normal turns.
4. Collapse visible direct-send bypasses behind an explicit persist-or-ephemeral API.
5. Tighten proactive/message source taxonomy before analytics/cadence learning depends on it.

This is not a rewrite. It is a small reliability spine pass around the new machinery.

## 7. Sources used

Local source/docs inspected:

- `AGENTS.md`
- `CLAUDE.md`
- `README.md`
- `codex/index.md`
- deleted prior-report content via `git show HEAD:codex/prompt_persona_deep_dive.md`
- deleted prior-report content via `git show HEAD:codex/top-system-review-and-roadmap-2026-05-23.md`
- deleted prior-report content via `git show HEAD:codex/architecture-review-2026-05-23.md`
- `agents/runtime.py`
- `agents/sdk_pool.py`
- `agents/telegram_bridge.py`
- `agents/messaging.py`
- `agents/postsend.py`
- `agents/hooks.py`
- `agents/proactive_gate.py`
- `agents/engagement/sender.py`
- `agents/background_listener.py`
- `agents/daily_checkin.py`
- `agents/stickers.py`
- `tools/approvals.py`
- `tools/photos/generate.py`
- `storage/db.py`
- `tests/test_final_sent_text_is_persisted.py`
- `tests/test_send_and_persist_api.py`
- `tests/test_proactive_session_isolation.py`
- `tests/test_run_user_turn_blocks.py`
- `tests/test_sdk_pool.py`
- `tests/test_media_outbox.py`
- `tests/test_proactive_global_reservation.py`
- `tests/test_ceremony_tg_id_propagation.py`
- `tests/test_proactive_persists_filtered_text.py`
- `tests/test_visible_proactive_is_recorded.py`
- `tests/test_inject_memory_cull.py`

External official source:

- Anthropic / Claude Code docs, [Claude Agent SDK sessions](https://code.claude.com/docs/en/agent-sdk/sessions), used only to confirm the general session/resume model.

Verification commands:

- `uv run python -m pytest tests/test_final_sent_text_is_persisted.py tests/test_send_and_persist_api.py tests/test_proactive_session_isolation.py tests/test_run_user_turn_blocks.py tests/test_sdk_pool.py tests/test_media_outbox.py tests/test_proactive_global_reservation.py tests/test_ceremony_tg_id_propagation.py -q`
- `uv run python -m pytest tests/test_visible_proactive_is_recorded.py tests/test_proactive_persists_filtered_text.py tests/test_health.py tests/test_start_and_reaction_event_rows.py tests/test_reactions_as_turns.py -q`
- `uv run python -m pytest -q`
- `uv run python -m compileall agents storage tools`
- `UV_CACHE_DIR=/private/tmp/hikari-uv-cache uv run python scripts/validate_tool_registry.py`
