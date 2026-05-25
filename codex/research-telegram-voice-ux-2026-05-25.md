# Research: Telegram + Voice UX for Hikari

Date: 2026-05-25  
Repo: `/Users/ol/agents/hikari-agent`  
Scope: research and product design only. No source-code changes.

## 1. Executive Summary

Hikari already has a solid Telegram bridge: owner gating, a command menu, typed and callback approvals, voice transcription, media/document ingestion, reminders, daily check-ins, proactive messages, tool/audit/status surfaces, and background task progress. The missing product layer is not "more commands"; it is a coherent work packet UX for multi-intent turns, especially voice notes.

The ideal Telegram + voice design is:

1. Treat each voice note as a turn bundle, not a single opaque prompt.
2. Segment the transcript into numbered tasks.
3. Classify each task as safe, inline-confirmable, typed-confirmation, or needs-clarification.
4. Send one compact acknowledgement: "heard 4 things; doing 3; 1 needs confirmation."
5. Avoid tool spam by using typing actions, one progress message for long work, and one final receipt.
6. Make commands global and diagnostic, buttons contextual and reversible, typed confirmation rare and reserved for high-risk actions.
7. Expose status and audit as first-class cockpit surfaces, not as hidden logs.
8. Recover from partial failure with a receipt that separates done, failed, pending, and retryable steps.

Telegram's Bot API supports the primitives Hikari needs: slash command menus through `setMyCommands`, inline keyboards and callback queries, voice/audio/document/file objects, and `sendChatAction` for "something is happening" indicators ([Telegram Bot API](https://core.telegram.org/bots/api), [setMyCommands](https://core.telegram.org/bots/api#setmycommands), [InlineKeyboardMarkup](https://core.telegram.org/bots/api#inlinekeyboardmarkup), [CallbackQuery](https://core.telegram.org/bots/api#callbackquery), [Voice](https://core.telegram.org/bots/api#voice), [Audio](https://core.telegram.org/bots/api#audio), [Document](https://core.telegram.org/bots/api#document), [File](https://core.telegram.org/bots/api#file), [sendChatAction](https://core.telegram.org/bots/api#sendchataction)).

Hermes Agent and OpenClaw both reinforce the same direction: messaging should be a gateway/channel layer with platform-specific affordances, task progress, permissions, and background work surfaces, while the core agent remains channel-agnostic ([Hermes Messaging Gateway](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/), [Hermes Voice Mode](https://hermes-agent.nousresearch.com/docs/user-guide/features/voice-mode/), [Hermes GitHub](https://github.com/NousResearch/hermes-agent), [OpenClaw Overview](https://docs.openclaw.ai/), [OpenClaw Telegram Channel](https://docs.openclaw.ai/channels/telegram), [OpenClaw Background Tasks](https://docs.openclaw.ai/automation/tasks), [OpenClaw GitHub](https://github.com/openclaw/openclaw)).

## 2. Current Telegram/Voice Behavior From Local Code

Local inspection covered the requested bridge, cockpit, handlers, gates, reminders, proactive paths, config, and tests.

Key current behavior:

- Telegram command menu is defined centrally in `agents/cockpit.py:26` and registered with Telegram in `agents/telegram_bridge.py:3045`.
- Incoming text messages are owner-gated in `agents/telegram_bridge.py:573`.
- Daily check-in replies are routed before approval handling in `agents/telegram_bridge.py:584`.
- Approval typed confirmations are consumed before a normal agent turn in `agents/telegram_bridge.py:616`; the approval module documents `CONFIRM-SEND` and timeout behavior in `tools/approvals.py:69`.
- Photo handling saves incoming images, appends compact events, can route OCR/classifier output to reminder/receipt/arxiv/link-save flows, and drains the media outbox after successful send in `agents/telegram_bridge.py:698`.
- Voice handling saves Telegram `.ogg`, enforces max duration, transcribes via `tools.voice.transcribe_voice`, appends a compact event, then wraps the transcript into a synthetic user prompt in `agents/telegram_bridge.py:819` and `tools/voice.py:1`.
- Document handling checks magic bytes, inlines safe text/PDF/image/HTML snippets with untrusted-content wrappers, and rejects unsupported or oversized files in `agents/telegram_bridge.py:1201` and `agents/telegram_bridge.py:1349`.
- Inline keyboards exist for approvals, daily check-in, and reminders in `agents/telegram_bridge.py:2314`, `agents/telegram_bridge.py:2322`, and `agents/telegram_bridge.py:2329`.
- Callback queries are owner-gated and routed by namespace in `agents/telegram_bridge.py:2424`.
- `/approvals`, `/proactive`, `/help`, `/status`, `/tools`, `/audit`, `/settings`, `/reminders`, and `/checkin` are handled in `agents/telegram_bridge.py:2065`, `agents/telegram_bridge.py:2135`, `agents/telegram_bridge.py:2240`, `agents/telegram_bridge.py:2461`, and `agents/telegram_bridge.py:2491`.
- Cockpit surfaces already format status, recent tools, audit logs, proactive recent/why, and settings in `agents/cockpit.py:271`, `agents/cockpit.py:396`, `agents/cockpit.py:451`, and `agents/cockpit.py:629`.
- Gatekeeper approvals are durable and recoverable: pending rows survive restarts, stale approvals expire, survivors can be re-nudged, and approved tool calls create audit rows in `tools/gatekeeper.py:48`.
- Reminder creation returns immediately and queues Google Calendar/Apple sync work in `tools/reminders/create.py:1`; due reminders are sent by scheduler/proactive code in `agents/proactive.py:193`.
- Daily check-in is a small state machine with schedule edits, pending reply window, yes/no intent parsing, email/calendar branches, and `NO_MESSAGE` guardrails in `agents/daily_checkin.py:1`.
- Proactive sending uses a global reservation gate for silence, quiet hours, dedup, empty text, and send failures in `agents/proactive_gate.py:1`; engagement source scoring, composition, sending, and guardrails live in `agents/engagement/selector.py`, `agents/engagement/composer.py`, `agents/engagement/sender.py`, and `agents/engagement/guard.py`.
- Background dispatch progress is already throttled to avoid chat spam in `agents/background_listener.py:1` and `agents/background_listener.py:115`.
- Config already has typing cadence, false-start behavior, proactive quiet hours and intervals, daily check-in, approval phrase/timeout, voice duration/STT settings, reminders, link shelf, Telegram timeout, and engagement source flags in `config/engagement.yaml`.

Relevant tests inspected:

- Telegram command menu: `tests/test_set_my_commands.py`
- Callback owner gating: `tests/test_callbacks_owner_gated.py`
- Daily check-in routing: `tests/test_daily_checkin_bridge.py`
- Voice STT: `tests/test_voice_stt.py`
- Media outbox: `tests/test_media_outbox.py`
- Approval preview truthfulness: `tests/test_approval_preview_truthful.py`
- Reminder tools and scheduler: `tests/test_reminders_tool.py`, `tests/test_reminders_scheduler.py`
- Proactive reservation and engagement events: `tests/test_proactive_global_reservation.py`, `tests/test_engagement_proactive_events.py`

Current gap: a voice note with several independent requests is treated as one transcript prompt. Hikari can execute multiple tools if the agent infers them, but Telegram has no explicit per-task packet, per-task status, or consolidated receipt model yet.

## 3. Internet Research Findings With Citations

### Telegram primitives

Telegram's official Bot API gives Hikari enough native surface area for a high-quality assistant:

- Command menus: bots can set command lists with `setMyCommands`; commands are compact global entrypoints, not the primary work surface ([Telegram setMyCommands](https://core.telegram.org/bots/api#setmycommands), [BotCommand](https://core.telegram.org/bots/api#botcommand)).
- Inline keyboards: messages can include button grids; buttons can carry `callback_data`, URLs, web apps, login URLs, or other special actions ([Telegram InlineKeyboardMarkup](https://core.telegram.org/bots/api#inlinekeyboardmarkup), [InlineKeyboardButton](https://core.telegram.org/bots/api#inlinekeyboardbutton)).
- Callback queries: every callback must be answered even if no visible alert is shown, which supports low-friction button UX without extra chat messages ([Telegram CallbackQuery](https://core.telegram.org/bots/api#callbackquery), [answerCallbackQuery](https://core.telegram.org/bots/api#answercallbackquery)).
- Voice/audio/files: Telegram distinguishes voice notes, audio files, documents, and downloadable file metadata, matching Hikari's current voice/photo/document handlers ([Telegram Message](https://core.telegram.org/bots/api#message), [Voice](https://core.telegram.org/bots/api#voice), [Audio](https://core.telegram.org/bots/api#audio), [Document](https://core.telegram.org/bots/api#document), [getFile](https://core.telegram.org/bots/api#getfile), [File](https://core.telegram.org/bots/api#file)).
- Progress presence: `sendChatAction` is explicitly for noticeable processing time and clears when the bot sends a message, making it better than repeated "still working" texts for short waits ([Telegram sendChatAction](https://core.telegram.org/bots/api#sendchataction)).

Design implication: use commands for global surfaces, inline buttons for local choices, and typing actions plus receipts for progress. Avoid emitting one Telegram message per tool unless the work is genuinely long-running.

### Voice and compound command UX

Voice UX research and product docs converge on a few principles:

- Google Home Routines model compound spoken triggers as a series of actions, including "good morning" examples that combine lights, weather, calendar, and news ([Google Home Routines](https://support.google.com/googlenest/answer/7029585)). Hikari should similarly treat a multi-task voice note as a decomposed action list.
- Apple Shortcuts frames automation as action sequences that can run from Siri and other surfaces ([Apple Shortcuts User Guide](https://support.apple.com/guide/shortcuts/intro-to-shortcuts-apdf22b0444c/ios)). Hikari's receipt should expose the same idea: ordered steps, inputs, outputs, and failures.
- Microsoft voice input guidance says voice is useful for cutting through complex interfaces, but commands should be concise, unambiguous, non-destructive, and backed by feedback showing what the system heard ([Microsoft Voice Input](https://learn.microsoft.com/en-us/windows/mixed-reality/design/voice-input)).
- Microsoft multiple-input guidance recommends supporting voice, touch, keyboard, and other inputs consistently rather than forcing one modality ([Microsoft Multiple Inputs](https://learn.microsoft.com/en-us/windows/apps/design/input/multiple-input-design-guidelines)). In Telegram, that means a voice note can start the work, buttons can confirm simple branches, and typed text can confirm risky steps.
- Google's conversation design confirmation guidance distinguishes explicit confirmation, implicit confirmation, and no confirmation; it recommends explicit confirmation for hard-to-undo actions and exact messages sent on the user's behalf ([Google Assistant Confirmations](https://developers.google.com/assistant/conversation-design/confirmations)).

Design implication: after STT, Hikari should explicitly reflect the parsed tasks when a voice note contains several actions, then execute safe steps and ask confirmations only where the cost of misunderstanding is high.

### Notification fatigue and proactive assistant UX

Proactive Hikari should be useful without becoming a second inbox:

- Apple notification guidance frames notifications as timely, high-value information, with user consent, Focus/scheduled delivery, and realistic urgency levels ([Apple Notifications](https://developer.apple.com/design/human-interface-guidelines/notifications), [Apple Managing Notifications](https://developer.apple.com/design/human-interface-guidelines/managing-notifications)).
- Recent notification studies report that ill-timed push notifications interrupt, annoy, and stress users, and that one-size-fits-all delivery is a poor fit for real daily contexts ([Echoes of the Day](https://www.mdpi.com/2076-3417/15/1/14)).
- A field experiment on communication-app notifications connects notification-caused interruptions with strain and performance concerns ([Effects of task interruptions caused by notifications](https://pmc.ncbi.nlm.nih.gov/articles/PMC10244611/)).
- IBM Research's proactive conversational-agent notification work proposes suppressing and aggregating notifications by severity, user preferences, and schedules to reduce alert fatigue ([IBM proactive notification system](https://research.ibm.com/publications/a-snooze-less-user-aware-notification-system-for-proactive-conversational-agents)).

Design implication: proactive Hikari should only speak when anchored, timely, and controllable. Every proactive should have "why this", "snooze", and "mute source" affordances, even if the actual copy is intimate and human.

### Approval and confirmation UX

Approval friction should be proportional to risk:

- Apple Alerts guidance says alerts interrupt the current task and should be used sparingly for critical information, destructive consequences, purchases, or other important actions; destructive actions should have a clear cancel path ([Apple Alerts](https://developer.apple.com/design/human-interface-guidelines/alerts)).
- Apple Action Sheets guidance separates intentional-choice surfaces from alerts and recommends a cancel path for choices that might destroy data ([Apple Action Sheets](https://developer.apple.com/design/human-interface-guidelines/action-sheets)).
- NN/g recommends confirmation dialogs before serious, hard-to-undo actions; warns against routine confirmations; requires specificity about consequences; and recommends a nonstandard action such as typing a word for especially dangerous operations ([NN/g Confirmation Dialogs](https://www.nngroup.com/articles/confirmation-dialog/)).
- Google's conversation design docs recommend explicit confirmation before difficult-to-undo actions and before sending exact text on a user's behalf ([Google Assistant Confirmations](https://developers.google.com/assistant/conversation-design/confirmations)).

Design implication: Hikari's current typed confirmation phrase is the right primitive, but high-risk actions should not be approvable by a single inline "confirm" tap. Buttons should reject, cancel, view details, edit, or retry. Typed confirmation should approve the dangerous side effect.

## 4. Hermes/OpenClaw Messaging Lessons

Hermes Agent:

- Hermes treats messaging as a gateway that connects many platforms and handles sessions, cron jobs, and voice delivery in one background process ([Hermes Messaging Gateway](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/)).
- Its platform table explicitly tracks per-channel capabilities such as voice, images, files, threads, reactions, typing, and streaming ([Hermes Messaging Gateway](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/)).
- It has chat commands for reset, model/personality, status, stop, approve/deny, voice, background work, usage, and more ([Hermes Messaging Gateway](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/)).
- It exposes busy-input modes, tool progress notification levels, and background sessions with final result delivery ([Hermes Messaging Gateway](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/)).
- The GitHub readme also frames CLI and messaging platforms as parallel entrypoints with shared slash command concepts ([Hermes GitHub](https://github.com/NousResearch/hermes-agent)).

Lesson for Hikari: Telegram should be a channel adapter with a strong command/callback/status contract. Long work should be background-capable and visible through task status. Voice should be an explicit mode/capability, not a hidden variant of text.

OpenClaw:

- OpenClaw describes itself as a self-hosted gateway across many chat apps, with sessions, routing, channel connections, media support, mobile nodes, and a web control UI ([OpenClaw Overview](https://docs.openclaw.ai/)).
- Its Telegram docs include native command registration, inline button capabilities, callback data passed back to the agent, edit/delete/react/send actions, and per-topic routing ([OpenClaw Telegram Channel](https://docs.openclaw.ai/channels/telegram)).
- Its task docs separate normal chat from background tasks, track `queued -> running -> terminal` states, expose task list/show/cancel/audit, and default many detached tasks to done-only or silent notification policies ([OpenClaw Background Tasks](https://docs.openclaw.ai/automation/tasks)).
- The GitHub repo is the official source for the implementation and release train ([OpenClaw GitHub](https://github.com/openclaw/openclaw)).

Lesson for Hikari: design a durable `WorkPacket`/task ledger and let Telegram render it compactly. Do not make chat history itself the only source of truth for work state.

## 5. Telegram Command/Menu Map

Principle: commands are global cockpit entrypoints. Natural language and voice do the actual work. Buttons resolve local choices.

Keep current commands:

| Command | Role | UX guidance |
| --- | --- | --- |
| `/help` | Discover commands | Short list only; no wall of docs. |
| `/status` | System health | Uptime, silence, jobs, MCP, OAuth, DB counts, cost, proactive state. |
| `/tools` | Tool surface | Recent tool calls and registered capabilities. |
| `/audit` | Trust surface | Recent audit, ID lookup, approvals, media. |
| `/settings` | Current config summary | Readable, not a giant YAML dump. |
| `/approvals` | Pending confirmations | Show pending high-risk actions and expiration. |
| `/reminders` | Reminder cockpit | List active reminders with snooze/dismiss buttons. |
| `/checkin` | Daily check-in control | Run now, skip tomorrow, show schedule. |
| `/proactive` | Proactive control | Status, why, recent, snooze, on/off. |
| `/silence` | Pause Hikari | Immediate muting. |
| `/unsilence` | Resume Hikari | Immediate resume. |
| `/tasks` | Open-loop memory tasks | Existing task list surface. |
| `/cancel` | Cancel pending work | Route to pending approval/work packet/background task. |
| `/memory` | Memory view | Keep low-frequency; not primary UX. |
| `/memory_diff` | Memory diff | Debug/trust surface. |

Recommended additions by phase:

| Command | Phase | Purpose |
| --- | --- | --- |
| `/work` | 2 | Show active work packets, background tasks, stuck steps, recent receipts. |
| `/receipt` | 3 | Show today's done/moved/learned/avoided slip and recent work receipts. |
| `/voice` | 4 | Show voice settings, last transcript, STT health, and "what can I say" examples. |

Command menu rules:

- Keep descriptions under Telegram's command description limit and existing test expectations ([Telegram BotCommand](https://core.telegram.org/bots/api#botcommand), `tests/test_set_my_commands.py`).
- Avoid adding every tool as a command. Hikari should understand "remind me", "save this link", "check tomorrow", and "draft this" naturally.
- Commands should never be the only path for a phone-first user.

## 6. Inline Button/Callback Map

Principle: buttons are for choices attached to the current message. They should not replace typed confirmation for high-risk external side effects.

Current callback families:

| Namespace | Current role | Keep/change |
| --- | --- | --- |
| `appr:*` | Approval confirm/reject/cancel | Keep reject/cancel/details. Move high-risk approve to typed-only. |
| `checkin:*` | Run now, skip tomorrow | Keep and expand to email/calendar/both choices. |
| `reminder:*` | Snooze/dismiss | Keep. Add custom snooze later if needed. |

Recommended callback families:

| Namespace | Example | Purpose |
| --- | --- | --- |
| `work:details:<id>` | `work:details:wp42` | Show full task packet and audit links. |
| `work:retry:<id>:<step>` | `work:retry:wp42:s3` | Retry one failed safe step. |
| `work:cancel:<id>` | `work:cancel:wp42` | Cancel pending or queued work. |
| `audit:view:<id>` | `audit:view:912` | Show audit entry without typing `/audit id 912`. |
| `pro:why:<id>` | `pro:why:88` | Explain why a proactive fired. |
| `pro:snooze:<source>:<dur>` | `pro:snooze:memory:2h` | Snooze a proactive source. |
| `pro:mute:<source>` | `pro:mute:calendar` | Disable source until settings change. |
| `link:kind:<id>:<kind>` | `link:kind:88:source` | Reclassify a saved link. |
| `daily:email` | `daily:email` | Daily check-in branch. |
| `daily:calendar` | `daily:calendar` | Daily check-in branch. |
| `daily:both` | `daily:both` | Daily check-in branch. |
| `daily:skip` | `daily:skip` | Skip current check-in. |

Callback rules:

- Keep callback payloads short because Telegram callback data is byte-limited ([Telegram InlineKeyboardButton](https://core.telegram.org/bots/api#inlinekeyboardbutton)).
- Always answer callback queries, even if the visible state is unchanged ([Telegram CallbackQuery](https://core.telegram.org/bots/api#callbackquery), [answerCallbackQuery](https://core.telegram.org/bots/api#answercallbackquery)).
- Keep the existing owner gate for all callback routes.
- A button may confirm low-risk/reversible actions. A button should not send email, delete data, publish, merge, or run write-enabled code.

## 7. Voice-Message Handling Flow

Current flow:

1. Receive Telegram voice.
2. Save `.ogg`.
3. Reject if too long.
4. Transcribe through OpenAI Whisper-compatible endpoint.
5. Run politeness/affect scan on transcript.
6. Add compact voice event.
7. Send one synthetic prompt to the live agent.
8. Persist reply after Telegram send succeeds.

Recommended flow:

1. Receive and transcribe as today.
2. If transcription fails, send a short recovery line and offer typed fallback.
3. Run a `VoiceTaskExtractor` over the transcript.
4. Build a durable `WorkPacket`:
   - `packet_id`
   - raw transcript
   - ordered steps
   - extracted phrase span per step
   - intent/tool hints
   - dependencies
   - risk tier: `safe`, `inline_confirm`, `typed_confirm`, `clarify`
   - status: `queued`, `running`, `succeeded`, `failed`, `waiting_confirm`, `skipped`
   - audit/tool IDs as they appear
5. Send one acknowledgement:

   ```text
   heard 4 things:
   1. remind you at 17:00 to call Maia
   2. save the link as useful
   3. check tomorrow's 10:00 meeting
   4. draft Sam an email

   doing the first three. the email stays as a draft until you confirm.
   ```

6. Execute safe independent steps.
7. Ask inline choices for reversible branches.
8. Ask typed confirmation for high-risk side effects.
9. Use `sendChatAction` while the work is short and one progress message if it becomes long.
10. Send one final receipt.

Segmentation rules:

- Split on conjunctions, pauses, temporal markers, "also", "and then", and tool-intent changes.
- Preserve order but allow independent safe steps to run concurrently behind one packet.
- Ask clarification before doing external side effects if the transcript is low-confidence, names are ambiguous, times are ambiguous, or the step involves sending/publishing/deleting.
- If a voice note contains more than about 7 tasks, ask the user to approve the parsed checklist before executing anything beyond safe local capture. This keeps the cognitive load humane.

## 8. Multi-Tool Progress Receipt Examples

### Multi-task voice note

User voice transcript:

```text
remind me at five to call Maia, save this link as useful, check if tomorrow's ten am meeting moved, and draft Sam an email saying i'll be ten minutes late
```

Initial acknowledgement:

```text
caught 4 things. doing the reminder, link save, and calendar check now.

the email will stop at draft until you confirm it.
```

Final receipt:

```text
done
1. reminder #143: call Maia at 17:00 today
2. link #88 saved as useful
3. calendar: "Design sync" is still tomorrow at 10:00

needs you
4. Gmail draft #42 to Sam is ready
   type CONFIRM-SEND 42 to send it

audit: /audit id 912
```

### Reminder + calendar + link save

User:

```text
remind me 30 minutes before the dentist tomorrow at 14:00, put it on my calendar, and save https://example.com as a source
```

Acknowledgement:

```text
on it: reminder, calendar mirror, and link save.
```

Receipt:

```text
done
1. reminder #144: dentist, 13:30 tomorrow
2. calendar event queued for sync: dentist, 14:00 tomorrow
3. link #89 saved as source

sync
Google Calendar: queued
Apple Reminders: queued

audit: /audit id 913
```

### Risky Gmail draft/send

User:

```text
send Nina an email saying we should cancel the contract unless legal signs off
```

Hikari should not send from one voice interpretation. It should show a preview:

```text
i drafted this, but sending needs typed confirmation.

to: Nina <nina@example.com>
subject: Contract sign-off

body:
We should cancel the contract unless legal signs off.

type CONFIRM-SEND 51 to send. nothing sends if you ignore this.
```

Buttons:

- `Reject`
- `Edit`
- `Audit`

No inline `Send` button for this tier.

### Daily check-in

Scheduled prompt:

```text
morning check-in: want inbox, calendar, or both?
```

Buttons:

- `Inbox`
- `Calendar`
- `Both`
- `Skip`

Receipt:

```text
check-in
inbox: 3 worth seeing
calendar: 2 fixed events, 1 soft hold

next: 09:00 tomorrow
```

### Proactive encouragement

Proactive message:

```text
you've had the Hikari Telegram report open for two days. ten ugly minutes on the conclusion would count.
```

Buttons:

- `Why this?`
- `Snooze 2h`
- `Mute this`

Why receipt:

```text
why this fired
source: active project loop
signals: repeated repo activity, no completed report receipt yet
quiet hours: clear
last proactive: 7h ago
```

### Partial tool failure

User:

```text
remind me to call Maia, add it to calendar, save this link, and send Sam the update
```

Receipt:

```text
done
1. reminder #145 created
2. link #90 saved

couldn't finish
3. calendar mirror failed: Google token expired
   retry after reconnecting Google

waiting
4. email to Sam is drafted but not sent
   type CONFIRM-SEND 52 to send

audit: /audit id 914
```

Buttons:

- `Retry calendar`
- `Reject email`
- `Audit`

## 9. Approval UX Design

Approval should be tiered:

| Tier | Action examples | UX |
| --- | --- | --- |
| No confirmation | Read-only search, calendar read, tool status, link search, memory recall, weather, simple calculations | Do it, then receipt if useful. |
| Implicit confirmation | Explicitly requested reminder create, link save, receipt add, local note capture | Do it and say what happened. |
| Inline confirmation | Snooze/dismiss reminder, skip check-in, choose link kind, retry safe failed fetch, cancel pending low-risk work, create non-inviting local calendar event from clear text | Inline buttons. |
| Typed confirmation | Send/reply email, public/published comments, calendar invites to others, event deletion, Drive delete/share/public upload, Notion destructive updates, GitHub merge/delete/release/public comments, dispatch with Edit/Write/Bash, shell/code with writes/network/secrets, bulk memory deletion | Preview exact effect and require typed phrase with ID. |
| Clarify first | Ambiguous recipient, ambiguous time, low STT confidence, multiple candidate people/events/files, missing URL, voice note with too many tasks | Ask one focused question. |

Typed confirmation rules:

- Use `CONFIRM-SEND <approval_id>` in the visible copy.
- Require the ID when more than one approval is pending. Prefer requiring it always.
- Preview critical fields before confirmation: recipients, subject, body, attendees, location, file paths, public/private status, repo/branch, command class, and irreversible consequence.
- Let `Reject` and `Cancel` be buttons.
- Let `Details` or `Audit` be buttons.
- Do not put a high-risk `Confirm` button beside `Reject`. NN/g specifically warns that routine confirmation clicks become automatic, and recommends nonstandard confirmation for particularly dangerous operations ([NN/g Confirmation Dialogs](https://www.nngroup.com/articles/confirmation-dialog/)).

What can use inline buttons:

- Reminder snooze/dismiss.
- Daily check-in branch choice.
- Proactive why/snooze/mute.
- Link kind selection.
- Safe retry after transient failure.
- Low-risk local capture.
- Reject/cancel/edit/details on a high-risk approval.

What needs typed confirmation:

- Anything that sends a message externally.
- Anything that deletes, publishes, merges, shares, purchases, invites, or runs write-enabled code.
- Anything where a mistaken STT result could expose private text to another person.

## 10. Failure/Recovery Copy

Principle: name what happened, name what did not happen, give the next action. Do not make the user infer safety.

STT failure:

```text
i couldn't transcribe that one cleanly. type the important bit and i'll pick it up.
```

Long voice note rejected:

```text
that voice note is longer than i can safely transcribe here. send the key tasks in a shorter note or paste the text.
```

Ambiguous recipient:

```text
i found two Sams. which one should get the draft?
```

Approval timeout:

```text
the send approval expired. nothing was sent.
```

External tool auth failure:

```text
the reminder is saved. Google Calendar sync failed because the token needs attention.
```

Partial success:

```text
two things are done; one failed; one is waiting on you.
```

Restart recovery:

```text
i lost the live run during restart, so i marked it failed instead of guessing. want me to re-dispatch it?
```

Repeated proactive ignored:

```text
i'll leave this thread alone for a while.
```

## 11. Notification/Proactive Rules

Current Hikari already has quiet hours, global reservations, dedup, silence checks, source intervals, and guardrails. The UX should make those controls visible.

Rules:

1. Every proactive must be anchored in a real signal: calendar edge, reminder, open task, saved link resurfacing, receipt pattern, repo activity, or explicit user preference.
2. Every proactive must be either actionable or emotionally useful. No generic "just checking in".
3. Every proactive should have a control path: `Why this?`, `Snooze`, or `Mute this source`.
4. Default to passive/digest delivery for low-urgency insights. Reserve immediate Telegram messages for reminders, calendar near-term changes, user-requested follow-up, or high-confidence support.
5. Never stack proactives. If two fire together, bundle them.
6. Use negative feedback: silence after a proactive, thumbs-down reaction, quick `/silence`, or repeated no-response should reduce source score.
7. Use positive feedback: thumbs-up, reply, completion, or follow-up question should increase source score.
8. Make `/proactive why` and button-level `pro:why` show the source, signals, quiet-hour check, last-send interval, and whether any candidates were suppressed.
9. Keep notification copy short. The detail belongs behind `Why this?` or `/audit`.

This matches platform guidance that notifications should be timely and high-value, and research showing that ill-timed notifications create stress and fatigue ([Apple Notifications](https://developer.apple.com/design/human-interface-guidelines/notifications), [Apple Managing Notifications](https://developer.apple.com/design/human-interface-guidelines/managing-notifications), [Echoes of the Day](https://www.mdpi.com/2076-3417/15/1/14), [IBM proactive notification system](https://research.ibm.com/publications/a-snooze-less-user-aware-notification-system-for-proactive-conversational-agents)).

## 12. Suggested Implementation Phases

Phase 1: Design and acceptance tests

- Land this report as product spec.
- Add tests for expected copy shapes and max-message counts.
- Decide exact approval tier policy.

Phase 2: WorkPacket model

- Add a durable `work_packets` table and `work_packet_steps`.
- Add a renderer for acknowledgement, progress, and final receipt.
- Add `/work` as read-only cockpit for active/recent packets.

Phase 3: Voice task extraction

- Keep current STT path.
- Add transcript segmentation before agent execution.
- Add risk classification and clarification routing.
- Preserve the raw transcript for audit and correction.

Phase 4: Progress aggregation

- For short work: typing actions only.
- For long work: one progress message or editable status message.
- For background dispatch: connect existing throttled progress to a `WorkPacket` receipt.

Phase 5: Approval tier hardening

- Keep typed `CONFIRM-SEND`.
- Require approval ID when multiple approvals are pending, preferably always.
- Remove high-risk inline confirm buttons.
- Keep inline reject/cancel/details/edit.
- Preserve existing restart recovery.

Phase 6: Proactive controls

- Add `Why this?`, `Snooze`, and `Mute source` callbacks to proactive messages.
- Surface source scores and suppressed candidates in `/proactive why`.
- Add source-specific fatigue budgets.

## 13. Suggested Tests/Evals

Voice and work packets:

- A transcript with four tasks creates four ordered steps.
- Safe independent steps run without waiting for typed confirmation.
- Risky steps stay pending and show exact preview.
- Ambiguous recipient/time produces clarification before side effects.
- Low-confidence STT produces a checklist confirmation.
- A long voice note still respects existing duration limits.

Spam control:

- A four-tool packet sends at most one acknowledgement, optional throttled progress, and one final receipt.
- `sendChatAction` is used for short processing instead of progress text.
- Background progress remains throttled.

Approval:

- High-risk actions cannot be approved by inline callback.
- Typed confirmation with wrong ID does not approve the wrong pending row.
- Approval preview includes critical Gmail, calendar, Drive, GitHub, Notion, dispatch, and command fields.
- Expired approvals say that nothing happened.

Callbacks:

- All callback routes stay owner-gated.
- Callback payloads remain under Telegram limits.
- Every callback query is answered.
- `work:*`, `pro:*`, `audit:*`, `daily:*`, and `link:*` route to expected handlers.

Receipts:

- Partial failure receipt separates `done`, `couldn't finish`, and `waiting`.
- Retry buttons only appear for retryable safe failures.
- Audit links resolve to redacted details.

Proactive:

- Quiet hours, silence, dedup, and source intervals suppress sends.
- `Why this?` explains source, signals, and suppression checks.
- Snooze/mute changes future source selection.
- Negative feedback reduces source score.

Daily check-in:

- Text replies and buttons both work.
- Skip tomorrow persists.
- Inbox/calendar/both choices produce concise final receipts.

Command menu:

- New command descriptions stay within Telegram and local test limits.
- `/help` remains compact.
- `/work` and `/receipt` are read-only until write actions are explicitly requested.

