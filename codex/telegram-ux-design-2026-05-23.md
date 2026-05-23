---
title: Telegram UX Design For Hikari Agent
date: 2026-05-23
repo: /Users/ol/agents/hikari-agent
scope:
  - commands
  - inline buttons
  - approvals
  - status
  - tools
  - audit
  - memory
  - proactive
  - settings
  - reminders
  - daily check-in
  - tool failure/status messages
---

# Telegram UX Design For Hikari Agent

## Executive Summary

Hikari should get a Telegram-native control surface before any Mini App or web
cockpit. The backend already has memory, proactive messages, reminders, daily
check-ins, gatekeeper approvals, tool registry policy, cost tracking, background
tasks, and audit rows. The missing product layer is not more power. It is fast,
visible control over the power that already exists.

The design goal is a small operator surface inside the chat:

- commands for durable entry points: `/status`, `/tools`, `/audit`, `/memory`,
  `/proactive`, `/settings`, `/approvals`, `/reminders`, `/checkin`;
- inline buttons for deterministic choices: approval details/rejection/copy
  phrase, daily check-in branches, reminder snoozes, proactive feedback,
  settings toggles;
- terse but actionable recovery copy for failed tools and integrations;
- a clear rule that buttons never hide risky writes. External sends, deletes,
  bulk operations, and code-dispatch writes still require typed confirmation
  unless explicitly downgraded later.

The first version should remain text-first and owner-only. Telegram buttons are
the cockpit; a Mini App is deferred until commands and buttons become too dense.

## Current Telegram Surface

### What Exists

`agents/telegram_bridge.py` is already a strong Telegram bridge:

- every message handler gates on `owner_id()`;
- outbound assistant text is filtered, sent to Telegram, then persisted with the
  final sent text and Telegram message id;
- a typing heartbeat starts before the agent call;
- daily check-in replies are pre-routed before approvals;
- gatekeeper approvals are resolved before normal chat;
- rude turns are refused before model dispatch;
- reactions are supported: thumbs up/down become feedback, other emojis can
  trigger a short Hikari turn;
- proactive sends use the same post-filtering and final-message persistence
  pattern as chat sends.

Registered commands today:

| Command | Current Behavior |
|---|---|
| `/start` | routes through the agent, drains photo outbox |
| `/silence [minutes]` | sets `runtime_state.silence_until`; defaults to `silence.default_minutes` |
| `/unsilence` | clears `silence_until` |
| `/tasks` | lists running and recent background tasks |
| `/cancel <task_id_prefix>` | marks a background task cancelled |
| `/cost` | shows daily chat plus dispatch cost |
| `/memory_diff <query>` | debug-only SQLite vs Graphiti recall comparison |
| `/approvals` | lists pending gatekeeper approvals |
| `/approvals cancel <id>` | cancels a pending gatekeeper approval |
| `/grab_stickers` | sticker capture operator command |

Current daily check-in behavior:

- scheduler fires a short "email? calendar?" question;
- the bridge stores a 30-minute pending reply window;
- text replies are parsed as both/email-only/calendar-only/skip;
- schedule edits like "check in at 06:30 tomorrow" are pre-routed;
- email/calendar fetches and summaries are sent as follow-up messages.

Current approval behavior:

- `can_use_tool` checks `config/tools.yaml`;
- `gatekeeper` writes an `approvals` row and sends a Telegram prompt;
- the user must type `CONFIRM-SEND` exactly to approve;
- explicit reject phrases are `cancel`, `stop`, and `abort`;
- other text auto-rejects the pending approval, then continues to normal chat.

Current proactive/reminder behavior:

- proactive events are stored with `source`, `pattern`, payload, Telegram
  message id, reaction feedback counts, and silence-window flags;
- cadence sources include open loops, pattern observations, noticings,
  calendar events, re-engagement, daily check-in, future letters, morning
  brief, wiki callbacks, lexicon callbacks, and recent episode callbacks;
- reminders fire as literal `reminder: <text>` messages and are then marked
  fired; repeat reminders insert the next occurrence.

### What Is Missing

The bridge has no `set_my_commands`, `CallbackQueryHandler`,
`InlineKeyboardMarkup`, callback token store, or user-facing `/status`,
`/tools`, `/audit`, `/memory`, `/proactive`, `/settings`, `/reminders`, or
`/checkin` command.

That creates three user-facing problems:

1. Capability discovery depends on reading code or remembering magic phrases.
2. Common decisions require typed replies even when the next step is
   deterministic.
3. Memory, tools, proactive sources, approvals, and integration health are
   internally rich but externally opaque.

## Command Map

Telegram command names must stay lowercase, digit/underscore-only, and 1-32
characters. The command menu should be registered at startup with
`setMyCommands` through python-telegram-bot, scoped to the owner/private chat
where possible, while handlers still verify `owner_id()` because Telegram
updates do not prove scope.

### Menu Commands

These should appear in Telegram's command menu:

| Command | Description | Status | Notes |
|---|---|---|---|
| `/start` | reintroduce Hikari and show quick controls | existing, enhance | Reply with compact intro plus status/settings buttons. |
| `/help` | show command and capability map | new | Keep short; link to hubs via buttons. |
| `/status` | show bot, scheduler, silence, integrations, approvals, cost | new | Main cockpit entry. |
| `/tools` | show connected tool families, auth, gates, recent failures | new | Productizes `config/tools.yaml`. |
| `/audit` | show recent tool calls, approvals, failures, proactive events | new | Read-only, redacted. |
| `/memory` | inspect, search, correct, forget memory | new | Replaces `/memory_diff` as the user surface. |
| `/proactive` | inspect and tune proactive sources | new | Source-level control. |
| `/settings` | global settings hub | new | Required by Telegram's global command convention when settings exist. |
| `/approvals` | pending/recent approval queue | existing, enhance | Add buttons and recent history. |
| `/reminders` | list and act on active reminders | new | Buttons for snooze/cancel/list. |
| `/checkin` | daily check-in schedule and actions | new | Small dedicated surface because check-in is a recurring ritual. |
| `/tasks` | background worker tasks | existing | Add retry/tail/cancel buttons later. |
| `/cost` | daily cost summary | existing | Link from `/status`. |
| `/silence` | quiet proactive messages | existing | Keep as fast coarse control. |
| `/unsilence` | resume proactive messages | existing | Keep as fast coarse control. |

### Hidden Or Debug Commands

These should remain callable but not menu-promoted:

| Command | Treatment |
|---|---|
| `/memory_diff` | debug-only; mention inside `/memory debug` for operator use |
| `/grab_stickers` | debug/operator; mention inside `/settings stickers` |
| `/cancel` | keep callable for exact task id prefixes, but `/tasks` should expose cancel buttons |

### Command Message Shape

Commands should render as compact plain text plus inline buttons, not as long
manuals. Example:

```text
status:
alive: yes
quiet: no
pending approvals: 1
proactive: on, 6 sent in 7d
tools: gmail ok, apple reminders pending permission, github ok
cost today: ~$0.13 / $5.00
```

Buttons:

```text
[approvals] [tools] [audit]
[memory] [proactive] [settings]
[refresh]
```

## Inline Button Design

### Principles

- Use inline keyboards for actions that should not add user messages to the
  chat: settings toggles, status navigation, approvals, reminder actions,
  daily check-in choices, proactive feedback.
- Keep typed confirmation for high-risk writes. Buttons can reject, show
  details, copy the confirmation phrase, snooze, or open settings; they should
  not silently send/delete/merge/bulk-write.
- Every callback must verify `callback.from_user.id == owner_id()` and should
  silently ignore or answer generically for non-owner callbacks.
- Every callback must call `answerCallbackQuery`, even when no visible toast is
  needed, because Telegram clients show a progress bar until it is answered.
- After a button changes state, edit the message text or reply markup instead
  of sending another message where possible.
- Button labels should be short verbs: `details`, `reject`, `snooze 1h`,
  `quiet today`, `forget`, `refresh`.
- Callback payloads must never contain PII, secrets, tool args, email subjects,
  calendar titles, or memory text.

### Callback Data Contract

Telegram `callback_data` is limited to 1-64 bytes, so store action details
server-side and send only a compact token.

Recommended callback data format:

```text
h1:<namespace>:<action>:<token>
```

Examples:

```text
h1:ap:reject:a8f32c
h1:st:refresh:live
h1:pr:snooze:9bd201
h1:mem:forget:f42
h1:rem:s1h:r17
```

Namespaces:

| Namespace | Surface |
|---|---|
| `ap` | approvals |
| `st` | status |
| `tl` | tools |
| `au` | audit |
| `mem` | memory |
| `pr` | proactive |
| `set` | settings |
| `rem` | reminders |
| `chk` | daily check-in |
| `tsk` | background tasks |

Use a callback-action ledger with:

- token;
- namespace/action;
- owner chat id;
- Telegram message id;
- resource type/id;
- payload JSON;
- status: pending/used/expired;
- created_at/expires_at.

Tokens should be single-use for state-changing actions and reusable for pure
navigation or refresh actions. Expired buttons should answer:

```text
expired. open /status again.
```

### Button Layouts

Status/navigation rows can use 2-3 buttons per row. Approval and destructive
actions should put the safe action first and the dangerous action visually
separate:

```text
[details] [reject]
[copy CONFIRM-SEND]
```

For Telegram clients/bot-library versions that support button styles, use
`danger` for reject/delete/bulk-delete and `success` for low-risk positive
actions. Do not rely on styling for safety; the server-side gate is the safety
boundary.

## Flow Specs

### Approval Flow

#### Entry Points

- out-of-band gatekeeper prompt from `GATEKEEPER.request()`;
- `/approvals`;
- `/approvals recent`;
- `/audit approvals`;
- inline `details`, `reject`, `copy phrase`, and later `allow 1h` buttons.

#### Prompt Shape

```text
approval #42
send email to alice@example.com: "schedule"

risk: external send
deadline: 4m 12s
type CONFIRM-SEND exactly to allow.
```

Buttons:

```text
[details] [reject]
[copy CONFIRM-SEND]
```

For bulk deletes, merges, repository deletes, file deletes, Python execution,
and outbound email, no `approve` button in P0. The typed phrase remains the
approval action.

For lower-risk repeat actions after P0, add an `allow 1h` button only when all
of these are true:

- tool is not destructive;
- tool is not external messaging;
- tool is not bulk mutation;
- args are not tainted by untrusted content;
- the prompt shows exactly which `tool_name` will be allowlisted;
- the action maps to the existing `always_approve(chat_id, tool_name, ttl)`.

#### Details View

`details` edits or replies with:

- tool family and exact tool name;
- redacted args preview;
- policy gate from `config/tools.yaml`;
- deadline;
- why the gate exists;
- source of request: chat, proactive, daily check-in, reminder sync, dispatch.

#### Resolution Rules

- Typed `CONFIRM-SEND` resolves through existing `resolve_pending_approval`.
- Button `reject` resolves the gatekeeper row as rejected and edits the prompt:
  `approval #42 rejected.`
- Button `details` does not consume approval.
- Any unrelated user text keeps current behavior: reject pending approval and
  pass the message to normal chat, but the copy should be clearer:
  `dropping approval #42. continuing with your message.`
- Expired approval buttons answer: `too late. ask again if it still matters.`

#### `/approvals`

Default view:

```text
pending approvals: 1
#42 send email to alice@example.com
deadline: 4m
```

Buttons:

```text
[#42 details] [#42 reject]
[recent] [refresh]
```

`/approvals recent` should show last 10 approval rows with status, tool,
created/resolved time, and result summary when present.

### `/status` Flow

`/status` is the owner cockpit. It should be read-only and fast.

Sections:

- live: process started, SDK session present, DB path, last user/assistant
  message time;
- scheduler: job ids and last/next run when available;
- chat state: silence_until, quiet hours, reaction-turn count/cap;
- proactive: enabled state, last proactive send, last 7d events by source,
  thumbs up/down, silence-within-1h count;
- daily check-in: enabled, default time, pending reply window, skip/override;
- approvals: pending count and nearest deadline;
- reminders: active count, next due, sync pending counts for Google/Apple;
- tools: integration health summary for Google, Notion, GitHub, Apple events,
  Apple Shortcuts, wiki, DuckDB, Playwright, YouTube transcript;
- memory: active facts count, open tasks count, recent episodes count;
- cost: today vs cap;
- failures: newest important tool/runtime failure, if any.

Buttons:

```text
[refresh] [approvals]
[tools] [audit]
[memory] [proactive]
[settings] [cost]
```

Copy examples:

```text
status:
alive. annoying, but alive.
quiet: off
pending approvals: 1
next reminder: 16:30 take meds
google: ok
apple reminders: permission pending
cost: ~$0.13 / $5.00
```

If a subsystem cannot be checked cheaply, show `unknown`, not `ok`.

### `/tools` Flow

`/tools` should make the registry legible without dumping raw YAML.

Default view:

| Family | Show |
|---|---|
| memory | read/write fact and task tools, no gate |
| wiki | search/read/list/tree are untrusted reads; append is ungated local write |
| utility | reminders/weather/calc/translate/notes/attachments/python |
| google | auth probe, write gates, recent 401/403 |
| notion | token configured, write gates |
| github | token configured, write gates |
| apple | EventKit/Notes/Shortcuts availability and permission state |
| browser/playwright | available, wildcard status |
| duckdb | query-only analytics status |

Subcommands:

| Command | Behavior |
|---|---|
| `/tools` | summary by family |
| `/tools health` | auth/env/probe status |
| `/tools policy` | gates and wildcard warnings |
| `/tools recent` | recent tool/audit entries |
| `/tools failures` | recent tool failures from audit/log-derived table |
| `/tools google` | Gmail/Calendar/Drive status and setup hint |
| `/tools apple` | Apple Notes/Reminders/Shortcuts permission status |

Buttons:

```text
[health] [policy]
[recent] [failures]
[google] [apple]
[refresh]
```

Policy rendering should group tools as:

- `read`: no approval, untrusted output wrapped when external;
- `local write`: allowed or gated depending on risk;
- `external write`: gatekeeper;
- `destructive`: gatekeeper plus typed phrase;
- `wildcard`: warning if upstream drift could add new tools.

### `/audit` Flow

`/audit` should answer "what did she just do?" without becoming a raw log tail.

Default view:

- last 10 meaningful events across:
  - approved gatekeeper tool calls from `audit_log`;
  - approval rows, including rejected/timeouts;
  - proactive events and feedback;
  - external wrap activations;
  - tool failures once a failure ledger exists;
  - reminder fires and sync failures;
  - background task state changes.

Subcommands:

| Command | Behavior |
|---|---|
| `/audit` | mixed recent activity |
| `/audit tools` | tool calls only |
| `/audit approvals` | pending/recent approval lifecycle |
| `/audit proactive` | proactive source events and feedback |
| `/audit failures` | failure-first view |
| `/audit verify` | check hash chain continuity |
| `/audit id <id>` | show redacted details for one row |

Buttons:

```text
[tools] [approvals]
[proactive] [failures]
[verify] [refresh]
```

Audit detail should always redact secrets and should truncate long args. If a
row includes untrusted-origin flags, show that explicitly:

```text
#81 github/create_issue
status: approved
args: {"repo":"owner/repo","title":"..."}
note: args contained untrusted web content; gatekeeper denied/required review.
hash: ok
```

### `/memory` Flow

`/memory` should turn memory into a visible, correctable product surface.

Memory surfaces:

- facts: durable bi-temporal rows in `facts`;
- episodes: summarized memories in `episodes`;
- open loops: `tasks`;
- core blocks: always-on state such as mood/check-in schedule;
- session transcript: final sent/received messages in `messages`;
- debug graph comparison: existing `/memory_diff`.

Default view:

```text
memory:
active facts: 143
open loops: 4
recent episodes: 3
last fact: #218 prefers terse UX reviews
```

Buttons:

```text
[recent facts] [open loops]
[search] [session search]
[debug diff]
```

Subcommands:

| Command | Behavior |
|---|---|
| `/memory recent` | newest active facts with ids |
| `/memory search <query>` | facts/episodes/session snippets |
| `/memory fact <id>` | fact detail, provenance, related facts |
| `/memory forget <id>` | invalidate active fact |
| `/memory correct <id> <new fact>` | insert replacement and mark old fact superseded |
| `/memory tasks` | open loops, with complete/drop buttons |
| `/memory session <query>` | transcript search once implemented |
| `/memory why <id>` | source/provenance and recall history for a fact |
| `/memory debug <query>` | wrapper around `/memory_diff` |

After important memory writes, Hikari should send a small undoable
confirmation:

```text
remembered #218: prefers terse UX reviews.
```

Buttons:

```text
[forget] [correct] [why]
```

`forget` should call the same invalidation path as `mark_fact_invalid`, not
hard-delete rows. `correct` should start a short reply flow, ideally using a
pending correction token and ForceReply-style prompt:

```text
send the replacement fact for #218.
```

The user can also type `/memory correct 218 prefers dense but readable specs`.

### `/proactive` Flow

`/proactive` should expose source-level controls over Hikari-initiated messages.
It should not replace `/silence`; `/silence` remains the emergency brake.

Default view:

```text
proactive:
global: on
quiet hours: 23:00-08:00
last sent: 2h ago, calendar_event
7d: 6 sent, 2 useful, 1 too much, 1 silence hit
```

Source rows:

| Source | Controls |
|---|---|
| `daily_checkin` | on/off, time, skip tomorrow |
| `morning_brief` | on/off |
| `calendar_event` | on/off, snooze |
| `open_loop` | on/off, cap |
| `pattern_observation` | on/off |
| `noticed_change` | on/off |
| `reengage_silence` | on/off |
| `wiki_new_file` | on/off |
| `recent_episode_callback` | on/off |
| `lexicon_callback` | on/off |
| `future_letter` | on/off |
| `reaction_followup` | on/off |

Buttons:

```text
[quiet today] [snooze all 3h]
[sources] [recent]
[daily check-in] [morning brief]
[refresh]
```

Subcommands:

| Command | Behavior |
|---|---|
| `/proactive` | summary |
| `/proactive sources` | source list with toggles |
| `/proactive recent` | recent events and feedback |
| `/proactive why` | explanation for latest proactive message |
| `/proactive off <source>` | disable source |
| `/proactive on <source>` | enable source |
| `/proactive snooze <source> <duration>` | snooze source |
| `/proactive quiet today` | silence all proactive until tomorrow morning |
| `/proactive reset` | clear snoozes, keep explicit off settings |

Proactive message buttons:

```text
[useful] [too much]
[why this] [snooze source]
[quiet today]
```

Button effects:

- `useful`: record positive feedback on the proactive event; equivalent to a
  more explicit thumbs up.
- `too much`: record negative feedback; optionally open source controls.
- `why this`: show source, pattern, trigger payload summary, cadence reason, and
  memory/fact ids when available.
- `snooze source`: snooze only that source for the default window.
- `quiet today`: set global silence until next local morning.

Recommended new persistence:

- `proactive_source_settings(source, enabled, snoozed_until, updated_at)`;
- richer `proactive_events.payload_json` with safe trigger details;
- optional `proactive_explanations` if payloads become too large.

### Daily Check-In Flow

Daily check-in should become the first buttonized flow because it already has a
deterministic branching parser.

Morning prompt:

```text
morning. inbox? calendar?
```

Buttons:

```text
[email] [calendar]
[both] [skip]
[snooze 30m] [settings]
```

Rules:

- Button callbacks should call the same email/calendar branches as parsed text.
- Text replies remain as fallback.
- The 30-minute pending window remains.
- If a check-in reply overlaps a pending approval, daily check-in continues to
  win because it is more specific and shorter-lived.
- `skip` should clear the pending state and edit the original keyboard to
  `skipped.`
- `snooze 30m` should schedule a one-shot check-in override today, not route
  through the normal agent.

Email digest buttons:

```text
[read personal] [nuke promos]
[later] [settings]
```

`nuke promos` must not delete directly. It should open a gatekeeper approval
with the Gmail query/count/sample ids in the details view and require typed
confirmation.

Calendar digest buttons:

```text
[prep next] [snooze calendar]
[ignore today]
```

`prep next` can ask Hikari to produce meeting prep only from visible calendar
data and memory. `snooze calendar` should disable calendar check-in follow-ups
for the day without disabling event reminders.

`/checkin` should provide:

| Command | Behavior |
|---|---|
| `/checkin` | current schedule and pending state |
| `/checkin now` | run the prompt immediately |
| `/checkin skip today` | skip today's check-in |
| `/checkin skip tomorrow` | skip tomorrow |
| `/checkin time 06:30` | set default local time |
| `/checkin tomorrow 08:00` | one-shot override |

### Reminder Flow

Reminder creation can remain natural language through the agent. The Telegram
surface should make existing reminders actionable.

Creation confirmation:

```text
reminder #17 set for 2026-05-23 16:30.
```

Buttons:

```text
[list] [cancel]
[snooze] [sync status]
```

`/reminders` default view:

```text
active reminders: 3
#17 16:30 take meds
#18 tomorrow 09:00 call mom
#19 weekly monday vitamins
```

Buttons per reminder:

```text
[#17 snooze] [#17 cancel]
[#18 snooze] [#18 cancel]
[refresh]
```

Fired reminder message:

```text
reminder #17: take meds
```

Buttons:

```text
[done] [snooze 10m] [snooze 1h]
[tomorrow] [drop]
```

Because current `fire_due_reminders()` marks one-shot reminders fired
immediately after send, post-fire snooze should create a new one-shot reminder
with the same text rather than mutating the fired row. For active reminders
that have not fired yet, snooze can call `reminder_snooze`.

For repeating reminders:

- `done` keeps the already-created next occurrence;
- `snooze 1h` creates a one-shot follow-up for this occurrence only;
- `drop` cancels the active future recurrence when one exists;
- later P1 can add `pause repeat` and `change repeat`.

### `/settings` Flow

`/settings` is a hub, not a giant preferences file.

Default buttons:

```text
[proactive] [daily check-in]
[memory] [reminders]
[tools] [reactions]
[stickers] [cost]
```

Settings surfaces:

| Surface | Controls |
|---|---|
| proactive | global on/off, quiet hours, source toggles, snoozes |
| daily check-in | enabled, default time, skip dates, pending window |
| memory | memory confirmations on/off, after-write undo buttons, session search visibility |
| reminders | default lead time, Google Calendar mirror, Apple Reminders mirror |
| tools | integration health, gate strictness notes, setup hints |
| reactions | reaction-turn enabled, feedback replies, cap/cooldown |
| stickers | enabled, capture mode, probability, mood blocklist |
| cost | daily cap display and warning threshold |

Settings toggles should use inline buttons and edit the message in place:

```text
daily check-in: on
time: 07:00
pending reply window: 30m
```

Buttons:

```text
[toggle] [change time]
[skip tomorrow] [back]
```

For text inputs like changing time, set a pending settings token and ask for a
reply. The input parser should be narrow and reject ambiguous times.

### `/tools`, `/audit`, And Tool Failure Messages

Tool failure/status messages should be short, in voice, and repairable. Do not
hide the actionable part behind personality.

Patterns:

| Situation | Copy Pattern | Buttons |
|---|---|---|
| Google token revoked | `gmail is locked out: invalid_grant. run scripts/setup_google_oauth.py.` | `[tools google] [retry probe]` |
| Calendar scope missing | `calendar refused the scope. oauth needs fixing.` | `[tools google]` |
| Apple automation denied | `apple reminders refused: <literal error>. macOS permission, not me.` | `[tools apple]` |
| Notion unauthorized | `notion says unauthorized. share the database with the integration.` | `[tools notion]` |
| GitHub token missing | `github token is missing. repo tools are read-dead right now.` | `[tools github]` |
| Tool timed out | `tool timed out after 15s. i didn't write anything.` | `[retry] [audit]` |
| Gatekeeper denied tainted args | `blocked: untrusted page text tried to ride along into a write.` | `[details] [audit]` |
| Reminder sync queued | `reminder set. calendar mirror is queued.` | `[sync status]` |
| Reminder sync failed | `reminder still fires here. calendar mirror failed: <reason>.` | `[retry sync] [details]` |
| Button expired | `expired. open /status again.` | none |
| Already handled | `already handled.` | none |
| Non-owner callback | answer callback generically or ignore; log only | none |

For high-risk failures, the copy must say whether an external side effect
happened. The most important distinctions:

- "didn't write anything";
- "sent, then failed to persist";
- "persisted locally, external mirror failed";
- "approved, but tool failed before execution";
- "approval expired, tool did not run".

## Inline Button Inventory

### Status

```text
[refresh] [approvals]
[tools] [audit]
[memory] [proactive]
[settings] [cost]
```

### Approvals

```text
[details] [reject]
[copy CONFIRM-SEND]
```

P1 safe repeat only:

```text
[allow this tool 1h]
```

### Daily Check-In

```text
[email] [calendar]
[both] [skip]
[snooze 30m] [settings]
```

### Email Digest

```text
[read personal] [nuke promos]
[later]
```

### Calendar Digest

```text
[prep next] [snooze calendar]
[ignore today]
```

### Proactive Messages

```text
[useful] [too much]
[why this] [snooze source]
[quiet today]
```

### Reminders

```text
[done] [snooze 10m] [snooze 1h]
[tomorrow] [drop]
```

### Memory Write Confirmation

```text
[forget] [correct] [why]
```

### Tools

```text
[health] [policy]
[recent] [failures]
[google] [apple]
```

### Settings

```text
[proactive] [daily check-in]
[memory] [reminders]
[tools] [reactions]
[stickers] [cost]
```

## Error And Recovery Copy Patterns

The copy should stay Hikari-shaped, but state machines need exactness. Use one
short line of voice, then the fix.

Good:

```text
gmail is locked out: invalid_grant. run scripts/setup_google_oauth.py.
```

Good:

```text
approval expired. nothing ran.
```

Good:

```text
reminder still fires here. google calendar mirror failed: 401.
```

Avoid:

```text
something went wrong.
```

Avoid:

```text
i can try again later.
```

Avoid:

```text
click Allow.
```

The project docs explicitly say there is no invented "click Allow" UI for
Apple/EventKit errors. Report the literal permission error and point to the
relevant `/tools apple` surface.

### Recovery Rules

- One retry button max for transient failures.
- Auth/setup failures should route to `/tools <family>`, not loop retries.
- If a callback fails after changing local state, show the local state and the
  failed external state separately.
- Every external write failure should create or update an audit/failure row.
- If Telegram send fails, do not persist the assistant row; this is already the
  bridge invariant and should remain.
- If a button action mutates state but editing the message fails, send a short
  fallback message and audit the edit failure.

## Implementation Order

This is design order only; no implementation is included in this report.

### Wave 1 - Telegram Infrastructure

1. Add command menu registration in startup/post-init.
2. Add a `CallbackQueryHandler` and owner-only callback dispatcher.
3. Add compact callback token storage with TTL, single-use state-changing
   actions, and reusable navigation actions.
4. Add shared render helpers for inline keyboards and expired/already-handled
   callbacks.
5. Add tests proving callbacks always answer Telegram and never route to
   normal chat.

### Wave 2 - Read-Only Cockpit

1. Implement `/help`.
2. Implement `/status` snapshot.
3. Implement `/tools` summary from `config/tools.yaml` plus existing health
   probes.
4. Implement `/audit` read-only views over approvals, audit_log, proactive
   events, reminders, and background tasks.
5. Keep all Wave 2 actions read-only except refresh/navigation.

### Wave 3 - Existing Flow Buttons

1. Add approval prompt buttons: details, reject, copy phrase.
2. Add `/approvals recent` and detail views.
3. Buttonize daily check-in: email/calendar/both/skip/snooze/settings.
4. Add reminder list/fired reminder action buttons.
5. Add task cancel buttons under `/tasks`.

### Wave 4 - Memory Ledger

1. Implement `/memory recent`, `/memory fact`, `/memory search`.
2. Add `forget` buttons using invalidation, not hard delete.
3. Add correction flow with pending reply token.
4. Add post-memory-write confirmation buttons.
5. Keep `/memory_diff` as debug-only.

### Wave 5 - Proactive Source Controls

1. Add source settings persistence.
2. Render `/proactive` status and source toggles.
3. Add proactive message buttons: useful, too much, why this, snooze source,
   quiet today.
4. Store richer explanation payloads for proactive events.
5. Add source feedback stats to `/status`.

### Wave 6 - Settings Polish

1. Build `/settings` hub.
2. Move daily check-in, proactive, memory confirmations, reminders mirrors,
   reactions, stickers, and cost thresholds into settings subviews.
3. Add narrow reply flows for settings that need typed values.
4. Reassess whether Telegram remains enough. Only then consider a Mini App.

## Test Plan

### Unit Tests

- Command menu list contains expected commands and excludes debug commands.
- Every command handler remains owner-only.
- Callback parser rejects malformed data, overlong data, wrong namespace, wrong
  owner, expired tokens, and reused single-use tokens.
- Every callback path calls `answerCallbackQuery`.
- Navigation callbacks are reusable; mutation callbacks are single-use.
- Callback payloads contain no raw email subjects, memory text, tool args, or
  secrets.

### Approval Tests

- Gatekeeper prompt renders details/reject/copy buttons.
- `reject` button resolves the DB row and in-memory event as rejected.
- `details` does not resolve the approval.
- Typed `CONFIRM-SEND` still approves.
- Expired approval buttons do not resurrect timed-out approvals.
- Tainted args still deny before any Telegram approval prompt.
- `/approvals recent` shows approved/rejected/timeout rows.

### Status/Tools/Audit Tests

- `/status` includes silence state, pending approvals, proactive counts,
  reminder counts, cost, and integration health.
- `/tools policy` groups tools by read/local write/external write/destructive
  and flags wildcard families.
- `/tools google` reflects healthy/unhealthy runtime probe states.
- `/audit verify` detects a broken hash chain in a fixture.
- `/audit failures` redacts secrets and truncates long args.

### Memory Tests

- `/memory recent` lists active facts with ids.
- `/memory forget <id>` marks a fact invalid and preserves history.
- Memory `forget` button is single-use.
- `/memory correct <id> <text>` inserts replacement and marks old fact
  superseded.
- Correction reply flow rejects empty/ambiguous replacement text.
- `/memory debug <query>` wraps existing SQLite/Graphiti comparison behavior.

### Proactive Tests

- `/proactive` renders source stats from `proactive_events`.
- `useful`/`too much` buttons update feedback counters.
- `quiet today` sets `silence_until` and marks recent proactive events as
  silenced when appropriate.
- `snooze source` prevents only that source from sending.
- `/proactive why` explains the latest proactive event from stored payloads.
- Reactions and explicit feedback buttons do not double-count unless the user
  uses both intentionally.

### Daily Check-In Tests

- Email/calendar/both/skip buttons call the same branches as parsed text.
- Check-in buttons clear or preserve `daily_checkin_pending` correctly.
- `snooze 30m` creates a one-shot override.
- Daily check-in still has priority over pending approval text replies.
- Ambiguous text still falls through to normal chat.

### Reminder Tests

- `/reminders` lists active reminders and renders action buttons.
- Active reminder snooze calls `reminder_snooze`.
- Fired reminder snooze creates a new one-shot reminder with the same text.
- `done` edits/removes buttons without changing already-fired one-shots.
- Repeat reminder `done` preserves the next occurrence.
- `drop` cancels the active future recurrence when one exists.
- Sync status distinguishes local reminder success from Google/Apple mirror
  failure.

### Integration/Manual Tests

- Use a separate Telegram test bot token for live UX checks before touching the
  real bot account.
- Verify command menu appears on Telegram mobile and desktop.
- Verify inline buttons edit messages cleanly on mobile and desktop.
- Verify `answerCallbackQuery` clears Telegram's spinner.
- Verify long status/audit messages stay under Telegram limits or paginate.
- Verify service restarts expire or recover pending callback/approval state.

## Sources

### Local Sources

- `AGENTS.md`
- `CLAUDE.md`
- `codex/index.md`
- `codex/ux-review-what-user-wants-2026-05-23.md`
- `codex/tool-priority-correction-2026-05-23.md`
- `codex/other-tools-review-2026-05-23.md`
- `agents/telegram_bridge.py`
- `agents/daily_checkin.py`
- `agents/proactive.py`
- `agents/scheduler.py`
- `tools/approvals.py`
- `tools/gatekeeper.py`
- `tools/gatekeeper_can_use_tool.py`
- `tools/reminders/create.py`
- `tools/reminders/list.py`
- `tools/reminders/cancel.py`
- `tools/reminders/snooze.py`
- `storage/db.py`
- `config/tools.yaml`
- `config/engagement.yaml`
- `tests/test_phase_f_gatekeeper_features.py`
- `tests/test_daily_checkin_bridge.py`
- `tests/test_proactive_feedback.py`
- `tests/test_reminders_scheduler.py`

### External Sources

- Telegram Bot Features: https://core.telegram.org/bots/features
  - command menu behavior, inline keyboards, menu button, global `/start`,
    `/help`, and `/settings`, testing with a separate bot.
- Telegram Bot API: https://core.telegram.org/bots/api
  - `BotCommand`, `setMyCommands`, `InlineKeyboardMarkup`,
    `InlineKeyboardButton`, `CallbackQuery`, `answerCallbackQuery`, and
    `editMessageReplyMarkup`.
