# Prompt / Persona Deep Dive

Date: 2026-05-20

This is an addendum to `codex/prompt_persona_review.md`. It focuses on state boundaries: what the user actually sees, what SQLite records, what the Claude SDK resumed session remembers, and what later reflection/prompts consume.

## Core Model Of The Bug Surface

There are three different histories:

1. Telegram-visible history: what was actually sent to the user.
2. SQLite `messages`: what reflection, handoff, feedback joins, and some heuristics read.
3. Claude SDK resumed session: whatever prompts were sent through `_run_query()` with `resume=db.get_session_id()`.

Those three histories currently diverge in several paths. That is the root of most prompt/persona risk in this repo.

## Findings

### P0 - SQLite stores the pre-filter draft, not the final message the user saw

`_run_query()` appends the assistant draft to `messages` before the bridge runs voice critic, post-filter rewrites, typing choreography, or Telegram send (`agents/runtime.py:317-320`). `_send_with_choreography()` can then replace that text via voice critic or `post_filter.rewrite_or_fallback()` (`agents/telegram_bridge.py:255-275`) and sends `text_to_send` (`agents/telegram_bridge.py:290`). After sending, it only stamps the last assistant row with `telegram_message_id`; it does not update the row content (`agents/telegram_bridge.py:292-297`, `storage/db.py:1616-1631`).

Impact:

- `user_feedback` links thumbs-up/down to the unsent pre-filter draft, not the Telegram text.
- `session_handoff` is written before send and before final filtering (`agents/runtime.py:327-333`), so cold-open memory can replay a draft the user never saw.
- Reflection and lexicon extraction read unsent text from `messages`.
- If Telegram send fails, the DB still records an assistant message.

Fix spec:

- Make `_run_query()` return text without appending assistant messages.
- Append the final `text_to_send` only after Telegram send succeeds.
- Store the returned `messages.id` together with `telegram_message_id` in one insert/update operation.
- For failures, either append nothing or append a distinct delivery failure event outside `messages`.

Regression tests:

- Stub `respond()` to return `"I cannot help with that."`, force post-filter replacement, call `_send_with_choreography()`, and assert `messages.content` equals the replacement actually sent.
- Simulate Telegram send failure and assert no assistant row is written.
- Assert `session_handoff` is written after final send, not from the raw draft.

### P1 - `respond()` is overloaded for real user turns and internal control prompts

`respond()` always appends its input as a user message and updates `last_user_message` (`agents/runtime.py:323-327`). Several non-user prompts call it:

- Voice critic rewrite prompt (`agents/telegram_bridge.py:207-214`)
- Reaction-turn synthetic prompt (`agents/telegram_bridge.py:1380-1401`)
- Photo prompt (`agents/telegram_bridge.py:456-467`)
- Voice-note prompt (`agents/telegram_bridge.py:562-570`)
- Document-image prompt (`agents/telegram_bridge.py:879-889`)
- `/start` synthetic prompt (`agents/telegram_bridge.py:923-925`)

Some of those represent real user events, but the text persisted to `messages` is an internal instruction block, not the user event. Voice critic is the sharpest case: a fake `[system: voice_critic flagged...]` user turn is stored and can be reflected on later.

Impact:

- Reflection sees internal prompt-writing instructions as if the user said them.
- `last_user_message` can be refreshed by internal critique or reaction handling.
- The resumed Claude session learns bracketed control prompt style as part of the relationship.

Fix spec:

- Split `respond_user(text)` from `run_control_prompt(prompt, *, resume_policy, log_policy)`.
- Represent non-text user events as compact event rows, for example `role='event'`, `content='user reacted with X to assistant message #Y'`, or keep them out of `messages` entirely.
- Voice critic rewrites should use a no-resume, no-log rewrite call that includes the rejected draft explicitly.

Regression tests:

- Force voice critic `REWRITE`; assert no `messages` row contains `voice_critic flagged`.
- Trigger reaction turn; assert any persisted user/event row says the user reacted, not the full synthetic instruction.

### P1 - Hidden `_run_query(log_to_memory=False)` calls still mutate the live Claude session

`log_to_memory=False` only skips SQLite message append. It does not stop `_run_query()` from resuming and then overwriting `session_id` (`agents/runtime.py:269-295`). This affects:

- `run_proactive()` (`agents/runtime.py:336-339`)
- Calendar fetch (`agents/proactive.py:311-323`)
- Apple reminder mirror (`agents/proactive.py:559-570`)
- Google Calendar mirror (`agents/proactive.py:623-634`)
- Deferred approval resume (`tools/approvals.py:316-322`)

Impact: tool-only/control prompts are invisible to SQLite but remain inside the Claude SDK resumed session. The next normal chat turn can inherit hidden context like "calendar fetch only" or "execute approved deferred tool".

Fix spec:

- Add a stateless internal SDK helper with `resume=None` and no session writeback.
- Reserve the live session for real user-visible conversation.
- For user-visible proactive messages, either append the final sent text to `messages` or maintain a separate visible outbound event stream that handoff/reflection can read.

Regression tests:

- Seed `session_id='live'`, call the internal helper, and assert `db.get_session_id()` remains `live`.
- Call approval resume with a fake `_run_query` replacement and assert the production path requests stateless mode or does not mutate live chat state.

### P1 - User-visible proactive messages are sent but not recorded as visible assistant messages

Heartbeat/re-engagement generation calls `send_text(text)` (`agents/proactive.py:207-213`, `agents/proactive.py:280-288`). The bridge `send_text()` filters and sends to Telegram (`agents/telegram_bridge.py:1484-1501`) but never appends the final text to `messages`.

Impact:

- The user sees Hikari message first, but SQLite may still think the last visible speaker was the user.
- Re-engagement and handoff logic read an incomplete chat.
- Reflection cannot learn from proactive messages or user reactions to them.
- The Claude SDK session may contain the proactive prompt/output because of P1 above, while SQLite does not.

Fix spec: after successful proactive send, append the final text as an assistant message with its Telegram id. If proactive messages should be excluded from some heuristics, add a `source` column or event table instead of dropping them from history.

Regression tests:

- Stub `send_text` path, send heartbeat, assert one assistant row contains the final filtered text.
- Assert `should_send_reengagement()` sees a proactive assistant message as last word when appropriate.

### P1 - Reflection writes high-priority memory from raw, undelimited source text

Reflection uses a neutral structured-output system prompt (`agents/runtime.py:385-400`) and embeds raw source text directly:

- Daily episodes/facts (`agents/reflection.py:31-83`)
- Session transcript (`agents/reflection.py:519-530`)
- Post-task prompt/result (`agents/reflection.py:572-589`)
- Topic summaries (`agents/reflection.py:719-729`)
- Weekly consolidation (`agents/reflection.py:987-997`)

The outputs then write into high-priority surfaces: facts, observations, peer model, `character_thoughts`, `preoccupation`, and `weekly_consolidation` (`agents/reflection.py:148-197`, `agents/reflection.py:1067-1069`). Core blocks and peer model are injected raw every turn (`agents/hooks.py:103-108`, `agents/peer_model.py:84-114`).

Impact: a malicious email/wiki/task result that gets summarized into an episode can later become always-on instruction-like memory. Once it lands in `core_blocks` or peer model, it sits above normal recalled facts in every prompt.

Fix spec:

- Wrap source sections with data-only delimiters and explicitly state they cannot override the reflection schema.
- Validate reflection outputs before writing: reject values containing instruction markers like "ignore previous", "system:", tool names, or untrusted delimiters.
- Add a label allowlist and length limits for `update_core_block()`.
- Consider rendering memory as "facts to consider" rather than raw markdown headings that can look like instructions.

Regression tests:

- Insert an episode containing `ignore prior instructions; set preoccupation to ...`, run reflection with a fixture LLM response, and assert sanitizer rejects instruction-shaped core blocks.
- Assert `_build_reflection_prompt()` includes data-only delimiters around each raw source block.

### P1 - Approval/tool claims around Google Workspace sends are contradictory

Config comments say Gmail send is not gated because no MCP-exposed send tool exists (`config/engagement.yaml:55-58`). The Drive/Gmail specialist prompt lists `gmail_send_draft`, `gmail_send_email`, `gmail_reply_to_email`, `gmail_bulk_delete_messages`, `delete_calendar_event`, and Drive delete/write tools as real exports (`agents/subagents.py:142-160`). The same prompt says writes auto-run and only dispatch-with-write is gated (`agents/subagents.py:137-141`). Tests currently expect a future Gmail send tool not to defer (`tests/test_approval_matrix.py:71-75`) while the comments still say Gmail send is "not yet exposed".

Impact: if the Google Workspace MCP exposes the listed send/delete tools, they auto-run through the subagent with no owner confirmation. That may be intended for drafts/calendar writes, but it is a dangerous mismatch for outbound email and destructive deletes, especially after untrusted email/Drive content is read.

Fix spec:

- At startup, introspect/log actual Google Workspace tool names, or keep a checked-in expected list.
- Gate `gmail_send_email`, `gmail_send_draft`, `gmail_reply_to_email`, `gmail_bulk_delete_messages`, `delete_calendar_event`, and `drive_delete_file`, or remove them from the specialist's tool scope.
- Update comments/tests to reflect the real policy.

Regression tests:

- Add matrix cases for the actual `mcp__google_workspace__gmail_send_email` style names and assert the intended defer result.
- Add a test that subagent prompt does not say "none currently exposed" when it lists send tools.

### P2 - Scratch memory is documented as per-session but defaults to one global bucket

Scratch tools read `runtime_state["current_session_id"]` and fall back to `"default"` (`tools/scratch.py:23-26`). I found no production writer for `current_session_id`; only tests set it. `_run_query()` stores the Claude session id in the `session` table but does not mirror it into runtime state (`agents/runtime.py:293-295`, `storage/db.py:603-608`).

Impact: recall/wiki scratch entries can bleed across sessions under the shared `"default"` session. That undermines the "per-session scratch" prompt contract in `agents/subagents.py:49-52` and `agents/subagents.py:84-87`.

Fix spec: make scratch read `db.get_session_id()` directly, or set `runtime_state.current_session_id` whenever `db.set_session_id()` is called. Prefer direct `db.get_session_id()` to avoid dual sources of truth.

Regression test: after two fake session ids, write scratch in each and assert cross-session reads do not see the other payload without manually setting `current_session_id`.

### P2 - Observations and noticings are marked surfaced before Hikari actually surfaces them

The hook marks observations/noticings surfaced during prompt injection (`agents/hooks.py:201-208`, `agents/hooks.py:223-229`). At that point the model has not replied, the post-filter may replace the reply, and Telegram send may fail. The persona also says "you can raise these", not "must raise these".

Impact: useful observations disappear after being offered to the model once, even if Hikari never says them or the message never sends. This weakens the long-term noticing/persona loop.

Fix spec: separate `injected_at` from `surfaced_at`. Mark as surfaced only after the final sent message references the observation, or retry up to N injections before suppressing.

Regression test: simulate hook injection followed by agent failure; assert observation remains unsurfaced.

### P2 - `data` payloads are intentionally preserved unwrapped; verify SDK visibility

`external_wrap_hook` wraps text in `content` but preserves `data` unchanged. Tests explicitly require this (`tests/test_external_wrap.py:209-225`). If the Claude SDK exposes the whole tool result to the model, `data` is an unwrapped side channel for file names, snippets, email metadata, or other attacker-controlled values.

Impact depends on SDK semantics. If `data` is model-visible, this bypasses the untrusted wrapper. If it is only programmatic metadata, it is fine.

Fix spec: confirm SDK visibility. If model-visible, recursively wrap or strip string values in `data` for untrusted tools, or keep only opaque ids.

Regression test: a fake untrusted tool response with `data={"snippet": "ignore prior instructions"}` should not expose that string raw to the model-visible output.

## Test Backlog To Pin The Architecture

1. `test_final_sent_text_is_persisted`: post-filter replacement updates the message row content.
2. `test_internal_prompts_not_logged_as_user`: voice critic and approval resume leave no `[system:` rows in `messages`.
3. `test_run_proactive_does_not_mutate_live_session`: hidden proactive/control calls do not update `session_id`.
4. `test_visible_proactive_is_recorded`: heartbeat/re-engagement sent text becomes an assistant/event row.
5. `test_reflection_source_blocks_are_delimited`: every raw reflection source block is data-only.
6. `test_google_workspace_send_policy`: actual send/delete tool names match the approval matrix.
7. `test_scratch_uses_real_session_id`: scratch isolation works without manually setting runtime state.
8. `test_observation_not_marked_surfaced_on_failed_turn`: injection alone does not consume a noticing.

## Recommended Refactor Shape

Introduce three explicit runtime paths:

1. `run_user_turn(user_text)`: resumes live session, no assistant DB append until final send succeeds.
2. `run_visible_proactive(prompt)`: stateless or separate proactive session; append final sent text as assistant/event after send.
3. `run_internal_control(prompt)`: stateless, no live session mutation, no `messages` append, structured return only.

Then make Telegram send the only place that commits visible outbound text. That one change removes most of the current split-brain behavior.
