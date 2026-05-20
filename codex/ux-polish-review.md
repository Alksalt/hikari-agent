# UX Polish Review - Hikari Agent

Date: 2026-05-19
Scope: Telegram/conversational UX, background task UX, proactive messages, multimodal flows, approval flows, and command discoverability.

This repo does not have a traditional visual frontend. The UX surface is the Telegram bridge plus the agent's timing, memory, proactive behavior, tool approvals, media handling, and background work feedback.

## Executive Summary

The app has a strong character layer and a lot of thoughtful safety rails, but the polish gaps are mostly trust and pacing issues:

- The user can be asked to approve an action with a 60s timeout that does not actually time out on the newer SDK-defer path.
- The bot often gives no visible feedback while the expensive part is happening, then adds a typing delay after the answer is already ready.
- The random ignore mechanic can eat real user intent and skip memory logging.
- `/cancel` says a task is cancelled even though the worker keeps running and may still report a final result.
- Calendar/proactive checks are enabled by default and can compete with normal chat turns, especially when Google Workspace is not configured.
- Hidden commands exist, but `/start` does not expose the operating surface.

If I were polishing this for daily use, I would do approval trust first, then latency feedback, then cancellation semantics, then proactive controls.

## P0 - Approval UX Can Lie About Timeouts

Finding: Deferred approvals display a timeout hint, but the SDK-defer path does not schedule a timeout watcher.

Evidence:
- `tools/approvals.py:67-73` renders "reply `y`..." / "type CONFIRM-SEND..." with a timeout.
- `tools/approvals.py:146-154` only the legacy approval path has `_timeout_watcher`.
- `agents/hooks.py:331-355` creates deferred approval rows and sends the prompt, but never schedules a watcher.
- `tools/approvals.py:156-206` leaves unmatched replies routed normally, keeping the approval pending.
- `agents/background_listener.py:187-222` resurfaces pending deferred approvals after restart, which confirms they can persist.

User impact:
- The prompt says "60s", but the approval can stay alive indefinitely.
- A user who ignores the approval may later type `y` in another context and accidentally approve stale work.
- "anything else to skip" is inaccurate for Tier 1 because most non-confirm replies are not consumed as rejection.

Polish:
- Add a timeout watcher for deferred approvals or remove the timeout hint from defer prompts.
- Change the Tier 1 hint to the actual behavior: "reply y to confirm, cancel to skip."
- Safer default: while an approval is pending, consume non-matching short replies with a clarification instead of silently routing them as normal chat.
- Add `/pending` or include pending approval in `/tasks`.
- Render human summaries instead of raw tool names and JSON args.

## P0 - The Bot Is Silent During The Slow Part

Finding: The typing indicator starts after the model response is already complete.

Evidence:
- `agents/telegram_bridge.py:233-241` awaits `respond(user_text)` first.
- `agents/telegram_bridge.py:95-138` sends `ChatAction.TYPING` and sleeps only after `reply_text` already exists.
- `agents/telegram_bridge.py:321-414` voice notes can spend time downloading/transcribing/responding before choreography begins.
- `agents/telegram_bridge.py:247-318` photo handling has the same issue.

User impact:
- On slow Claude turns, STT, photo analysis, or tool use, the chat sits dead with no feedback.
- Then the bot waits again for a fake typing delay, making latency feel worse.

Polish:
- Start a typing heartbeat immediately when a user message, photo, or voice note is accepted.
- Keep refreshing typing every ~4 seconds until the response is ready.
- After the model returns, cap the final artificial delay to something tiny if the user already waited.
- Use separate status beats only for long work: "listening." for voice, "looking." for photo, "working." for dispatched sessions.

## P1 - Random Ignore Can Eat Real Intent

RESOLVED: 2026-05-20 — `should_ignore`, `_ignore`, and the `ignore:` config block deleted entirely. The bridge now always routes to the LLM. Action lines remain available as voice devices in Hikari's character layer.

Finding: `should_ignore` can bypass the agent entirely, including for potentially important messages.

Evidence:
- `agents/telegram_bridge.py:226-231` rolls ignore before `respond`.
- `agents/bridge_ux.py:69-94` sends only an action line and returns.
- `config/engagement.yaml:26-38` has ignore probabilities up to 25 percent in irritable mood.

User impact:
- Important messages can get `[ignores]`.
- Ignored messages are not appended through `respond`, so they are missing from normal conversation memory.
- Reaction can fire before ignore (`agents/telegram_bridge.py:220-231`), creating odd combinations like reacting to a serious message and then refusing to answer.

Polish:
- Never ignore messages with urgency, commands, questions, media follow-ups, approvals, error reports, or emotional-half-life triggers.
- Record ignored inbound text as a user message or a lightweight episode with `ignored=true`.
- Cap ignore to once per day or once per session, not only streak/cooldown.
- Prefer "delayed response" over full ignore for actionable messages.

## P1 - `/cancel` Is Not Real Cancellation

Finding: `/cancel` marks the row cancelled, but the in-process worker continues.

Evidence:
- `agents/telegram_bridge.py:522-550` tells the user the task is marked cancelled and will finish its current turn.
- `tools/dispatch.py:114-185` never checks for cancelled status while consuming SDK output.
- `agents/background_listener.py:123-135` will still send a done message if the worker emits `done`.

User impact:
- The user can cancel and still get a completion message later.
- Cost may continue after the user believes they stopped it.

Polish:
- Store `task_id -> asyncio.Task` and cancel the task directly when possible.
- In `_run_session`, check DB status between messages/tool-use events and stop if cancelled.
- If true cancellation is impossible, rename the UX: "marked stale; i'll ignore the result."
- Suppress done/failed messages for cancelled tasks unless they are explicitly labelled as post-cancel cleanup.

## P1 - Calendar Heartbeat Is Too Eager By Default

Finding: calendar heartbeat is enabled by default and polls every 5 minutes through the LLM/subagent path.

Evidence:
- `config/engagement.yaml:488-501` enables `calendar_heartbeat`.
- `agents/scheduler.py:40-44` schedules it every configured interval, default 5 minutes.
- `agents/proactive.py:304-323` fetches events by calling `run_proactive`.
- `agents/runtime.py:33-40` serializes all `_run_query` calls through one lock.
- `README.md` says Google Workspace is wired but stubbed until credentials are configured.

User impact:
- If Google Workspace is not configured, the scheduler may still burn attempts and logs.
- Calendar polling can contend with normal chat turns through the shared runtime lock.
- The user may experience random chat latency from a background job they did not explicitly enable.

Polish:
- Disable calendar heartbeat automatically unless Google credentials are present and the MCP server is healthy.
- Move calendar fetch to a direct API/tool call where possible, not a full proactive LLM call every 5 minutes.
- Add a health flag visible via `/status` or `/cost`.
- Do not run proactive LLM work while a foreground user turn is waiting.

## P1 - Offline Messages Are Dropped

Finding: polling starts with `drop_pending_updates=True`.

Evidence:
- `agents/telegram_bridge.py:658` calls `app.run_polling(drop_pending_updates=True)`.

User impact:
- If the bot is offline or restarting while the user texts, those messages disappear.
- For a one-person companion/chat UX, silent message loss feels worse than delayed replies.

Polish:
- Set `drop_pending_updates=False` for normal operation.
- If duplicate backlog is a concern, add a startup drain policy that summarizes old messages or ignores only messages older than a configured threshold.

## P1 - Command Discoverability Is Thin

Finding: operational commands exist but are not discoverable in the chat UX.

Evidence:
- `agents/telegram_bridge.py:583-588` registers `/start`, `/silence`, `/unsilence`, `/tasks`, `/cancel`, `/cost`.
- `agents/telegram_bridge.py:450-468` routes `/start` through persona only and does not expose capabilities.
- There is no `/help`, `/status`, or Telegram command menu setup.

User impact:
- The user may not remember how to silence, inspect, cancel, or check cost.
- The app has operational power, but no compact control surface.

Polish:
- Add `/help` that is in voice but concrete.
- Add `/status`: silence state, pending approval, running tasks, proactive count, calendar health, cost today.
- Add Telegram bot commands on startup if python-telegram-bot supports it in this stack.

## P2 - Persona Filter Still Ships Long Drift

Finding: long assistant-voice or sycophancy drift is detected but not rewritten by default.

Evidence:
- `agents/telegram_bridge.py:115-123` logs `needs_llm_rewrite` but still ships the original text.
- `config/engagement.yaml:170-177` has `enable_llm_rewrite: false`.

User impact:
- The most damaging failures for this product are not always short refusal leaks. Long generic assistant replies can break the illusion harder.

Polish:
- Enable rewrite for long drift once the rewrite prompt is stable.
- Add a deterministic fallback for common long assistant preambles.
- Track drift score alongside actual sent text and surface a daily summary in logs or `/status`.

## P2 - Important Memory Signals Can Be Marked Surfaced Before They Are Used

Finding: observations and noticings are marked surfaced during hook injection, before the agent actually mentions them.

Evidence:
- `agents/hooks.py:140-147` marks observations surfaced while formatting context.
- `agents/hooks.py:162-168` marks noticings surfaced while formatting context.
- `agents/hooks.py:101-105` consumes handoff context before knowing whether it shaped the reply.

User impact:
- A meaningful noticing can be consumed by a failed turn, ignored draft, or unrelated response.
- The system loses chances to surface high-value continuity.

Polish:
- Mark as "offered" at injection and "surfaced" only after the outbound reply uses or references it.
- Simpler version: keep offered items eligible for one more turn unless the model response contains a relevant phrase or the turn succeeds.

## P2 - Background Task Updates Mix Character With Raw System Telemetry

Finding: progress and completion messages expose tool-use counts and raw worker summaries.

Evidence:
- `agents/background_listener.py:47-52` appends `({tool_uses} tool uses so far)`.
- `agents/background_listener.py:55-64` sends time/cost plus a raw summary snippet.
- `tools/dispatch.py:235-238` returns a raw dispatch acknowledgement that the lead agent has to rewrite.

User impact:
- Useful, but it can feel like switching from Hikari to a job runner.
- Raw summaries can be too long, too assistant-like, or not action-oriented.

Polish:
- Keep task telemetry available in `/tasks` and make proactive pings cleaner.
- Final message should be: result, changed files if any, tests if any, next blocker if any.
- Use a fixed completion renderer instead of shipping arbitrary worker prose directly.

## P2 - Timezone Behavior Is Not Explicit Enough

Finding: scheduler timezone and "local" comments do not line up cleanly.

Evidence:
- `agents/scheduler.py:25` creates `AsyncIOScheduler(timezone="UTC")`.
- `agents/scheduler.py:63-66` comment says daily reflection is 09:00 local, but the scheduler is UTC.
- `agents/proactive.py:52-59` quiet hours use naive `datetime.now().time()`, which depends on host local timezone.

User impact:
- Quiet hours, daily reflection, and proactive cadence can fire at surprising local times depending on deployment host timezone.

Polish:
- Add `timezone` to config and use it everywhere.
- Make `/status` show current local time, quiet-hours state, next scheduled jobs.

## P2 - Long Telegram Sends Have No Split/Retry Path

Finding: outgoing text is sent as one `reply_text`.

Evidence:
- `agents/telegram_bridge.py:138` sends `message.reply_text(text_to_send)` directly.
- The same pattern exists for many command/error sends.

User impact:
- Long code/data replies or oversized tool summaries can fail Telegram limits and leave the user with nothing.

Polish:
- Centralize send into `send_text_chunks`.
- Split near Telegram's limit, preserve code blocks when possible, and send a short failure if chunking fails.
- Apply the same wrapper to command replies, proactive sends, approvals, and background listener sends.

## P2 - Politeness Gate Needs More Context

Finding: rude-pattern matching is deterministic and runs before the agent can interpret context.

Evidence:
- `agents/telegram_bridge.py:183-193` refuses immediately.
- `config/engagement.yaml:181-186` flags patterns like "hurry up" and "do this now".

User impact:
- A stressed but legitimate message like "hurry up, meeting in 5" gets a refusal instead of urgency handling.

Polish:
- Keep hard insults deterministic, but route urgency/commanding phrases through the agent with an instruction rather than blocking.
- Add a "real deadline" bypass when the message includes time pressure.
- Log and test false positives from real chat snippets.

## P3 - Multimodal Flows Need Small Ergonomic Passes

Finding: voice/photo/location support works conceptually, but the edge feedback is thin.

Evidence:
- `agents/telegram_bridge.py:321-414` voice note flow only replies after download, STT, model, and choreography.
- `agents/telegram_bridge.py:247-318` photo flow behaves similarly.
- `config/engagement.yaml:522-525` defines `photo_in.caption_max_chars`, but `agents/telegram_bridge.py:272-299` does not enforce it.
- `tools/location.py:116-155` uses an inbound counter defer, but only text messages reliably bump the counter through reactions.

Polish:
- Send immediate typing for voice/photo.
- Add specific failure messages for missing API key vs file too long vs transcription empty, still in voice.
- Enforce caption length or summarize long captions before prompt construction.
- Make location defer independent from reaction/inbound counters, or bump inbound count for all inbound modalities.

## P3 - Proactive Max Interval Is A Dead-Looking Knob

Finding: `heartbeat_max_interval_hours` exists in config but is not used in heartbeat eligibility.

Evidence:
- `config/engagement.yaml:40-47` defines min and max.
- `agents/proactive.py:66-90` uses only min interval and other gates.

User impact:
- Operators may tune a max interval expecting behavior that never happens.

Polish:
- Either implement a max interval as a stronger nudge after long silence, or remove/rename the knob.

## Recommended Implementation Order

1. Approval v2:
   - deferred timeout watcher
   - accurate prompt copy
   - pending approval status in `/tasks` or new `/status`
   - safer non-match handling

2. Foreground feedback:
   - immediate typing heartbeat
   - shared send wrapper with chunking
   - reduced post-response artificial delay after slow turns

3. Task control:
   - real cancellation or honest "ignore result" semantics
   - suppress post-cancel completion pings
   - clearer `/tasks` rows

4. Proactive controls:
   - disable calendar heartbeat unless configured healthy
   - foreground-turn priority over background LLM jobs
   - timezone config

5. UX regression harness:
   - fake Telegram bot/update objects
   - seeded randomness
   - transcript tests for approval, ignore, cancel, voice failure, photo failure, calendar disabled, proactive silence, long reply chunking

## Suggested UX Regression Scenarios

- User sends a normal request and sees typing within 1 second while the model is still running.
- User sends a long voice note with missing `OPENAI_API_KEY` and gets a specific, short failure.
- User approves a deferred wiki write after timeout and it does not execute.
- User says "cancel" while approval is pending and the approval is rejected, not routed to chat.
- User says "hurry up, meeting in 5" and the bot helps instead of politeness-refusing.
- User sends urgent text while irritable mood ignore probability is high and the message is not ignored.
- User cancels a background task and never receives a normal "done" message for it.
- Bot restarts while messages are pending and does not silently drop them.
- Google Workspace credentials are absent and calendar heartbeat does not call the LLM.
- Long response is chunked and all chunks arrive.

## Bottom Line

The biggest UX polish is not more personality. The personality is already strong. The next layer is operational trust: when the app says "timed out", "cancelled", "quiet", "working", or "approved", those words need to map exactly to system behavior.
