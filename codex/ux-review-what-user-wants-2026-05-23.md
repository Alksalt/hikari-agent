# UX Review: What The User Probably Wants Next

Date: 2026-05-23
Scope: local app review first, then internet research, then UX recommendations.

## Executive Read

Hikari's backend is already much stronger than the product surface suggests. The app has memory, tools, proactive cadence, daily check-ins, voice/photo/document handling, approval gates, reactions, and careful final-message persistence. The weak point is not core capability. The weak point is that the user has to discover and steer most of it through plain chat.

The likely user need is: fewer generic companion pings, more grounded "I noticed X, want me to act?" moments, and clear controls for memory, proactive sources, approvals, tools, cost, and integration health.

The biggest missing UX layer is a small control surface. This can start entirely inside Telegram with bot commands and inline buttons. A Telegram Mini App or local web cockpit can come later.

## App Review

### What Exists And Works

- The app is a Telegram-first personal companion, not a web UI. I did not find a local frontend target (`package.json`, React/Vite/Next files, HTML/CSS app files) to open in a browser.
- Outbound messages are filtered, sent, and only then persisted, which protects continuity from phantom assistant rows. See `agents/telegram_bridge.py:179-270`.
- The bridge already prioritizes special flows before normal chat: daily check-in replies, pending approvals, politeness guard, affect scanning, reactions, and belief-frame handling. See `agents/telegram_bridge.py:360-441`.
- Working memory is now injected from recent turns, with explicit data framing. See `agents/hooks.py:116-157` and `config/engagement.yaml:502-505`.
- Proactive cadence is source-aware internally, with separate pools for spontaneous, scheduled, and user-anchored messages. See `config/engagement.yaml:473-500`.
- Proactive seeds are grounded in open loops, pattern observations, noticings, callbacks, and recent episodes before falling back. See `agents/proactive.py:107-145`.
- Re-engagement is narrow and rate-gated: only if Hikari had the last word and the user has been silent 2-6 hours. See `agents/proactive.py:299-345`.
- Daily check-in can ask about email/calendar, summarize personal mail, surface calendar invites, and propose deleting promo/update mail. See `agents/daily_checkin.py:477-535`.
- Callback surfacing already attempts to bring back high-importance recent episodes when topically relevant. See `agents/callback_surface.py:1-121`.

This is a good foundation. It means the next UX gains should come from exposing, controlling, and tightening existing behavior rather than adding more raw agent powers.

### What Is Wrong Or Missing

1. **Capability discovery is too hidden.**

   The bot only registers these commands: `/start`, `/silence`, `/unsilence`, `/tasks`, `/cancel`, `/cost`, `/memory_diff`, and `/grab_stickers` (`agents/telegram_bridge.py:1739-1753`). I found no bot command menu setup, no `/help`, no `/status`, no `/settings`, no `/proactive`, and no normal-user memory command.

   For the owner-builder, this creates a mismatch: the app can do a lot, but the user has to remember magic phrasing or read code/docs.

2. **There are no Telegram-native buttons for common decisions.**

   I found no `InlineKeyboardMarkup`, callback query handling, reply keyboard, command menu setup, or menu button usage in `agents`, `tools`, `config`, or `tests`.

   This makes low-friction choices harder than they need to be:

   - daily check-in asks "email? calendar?" but requires text parsing
   - approvals require typed resolution
   - proactive pings do not offer "open", "snooze", "quiet today", or "wrong source"
   - reminder actions do not expose common buttons like "done", "snooze 1h", or "tomorrow"

3. **Proactive behavior is internally governed but externally opaque.**

   The code has source tags and cadence pools, but the user cannot easily ask:

   - what proactive sources are enabled?
   - why did you message me?
   - how often did each source fire this week?
   - snooze only Gmail nudges, but keep calendar prep
   - disable generic re-engagement, keep useful task/calendar/wiki triggers

   `/silence` and `/unsilence` are too coarse for a personal agent that has many source types.

4. **Memory is product-critical but not inspectable as a product.**

   The app has memory systems and a debug command `/memory_diff`, but there is no user-friendly memory ledger:

   - "what do you remember about me?"
   - "what did you learn this week?"
   - "why did you mention that?"
   - "forget that"
   - "correct this fact"
   - "pin this preference"

   This is a trust problem, not just a feature gap. A personal companion that remembers should make remembered facts visible and correctable.

5. **Approvals and tool activity are not legible enough.**

   Approval handling exists, but there is no obvious `/approvals` or `/tools recent` surface for pending, accepted, denied, or failed actions. The user should not have to infer what the agent is blocked on, which connector failed, or what exactly was sent/read/deleted.

6. **Error recovery likely feels like personality, not repair.**

   The bridge prevents raw SDK errors from leaking and swaps in in-voice fallback text. That is good for tone, but some failures need actionable recovery:

   - Gmail credential missing
   - Calendar scope missing
   - Telegram send failed
   - Apple automation permission denied
   - memory write failed
   - proactive event vetoed

   For an owner-builder, the best UX is short and in-voice, but still tells them the exact fix or command.

7. **The app has no "operator cockpit."**

   The user probably wants to know whether Hikari is awake, what she is watching, what she recently did, and what is broken. Right now that is scattered across logs, SQLite, config, and code.

   This does not need to be a big web app. A Telegram-first cockpit can be enough at first.

## Research Notes

The research supports the same direction: make capabilities visible, support correction, make notifications actionable and controllable, and use the platform's native controls.

- Telegram's bot docs explicitly support command menus, inline keyboards, callback buttons, and Web Apps. Inline keyboards are intended for settings, toggles, and navigating results without forcing messages into the chat. Sources: Telegram Bot Features (`https://core.telegram.org/bots/features`) and Bot API (`https://core.telegram.org/bots/api#inlinekeyboardmarkup`, `https://core.telegram.org/bots/api#setmycommands`).
- OpenAI's memory UX sets the expectation that memory can be reviewed, deleted, searched/sorted, corrected, and connected to memory sources. Source: OpenAI Memory FAQ (`https://help.openai.com/en/articles/8590148-memory-faq`).
- Apple's notification guidance emphasizes timely, high-value information, consent, urgency levels, and user control over interruptions. Source: Apple Human Interface Guidelines, Notifications and Managing Notifications (`https://developer.apple.com/design/human-interface-guidelines/notifications`, `https://developer.apple.com/design/human-interface-guidelines/managing-notifications`).
- Microsoft's Human-AI Interaction Guidelines recommend making clear what the system can do, showing contextually relevant information, supporting efficient dismissal/correction, explaining why the system acted, encouraging granular feedback, providing global controls, and notifying users about changes. Source: Microsoft Research (`https://www.microsoft.com/en-us/research/articles/guidelines-for-human-ai-interaction-eighteen-best-practices-for-human-centered-ai-design/`).
- Hermes Agent presents memory, tools, scheduled tasks, and sessions as visible product concepts rather than invisible internals. This is a useful comparator for Hikari because both are long-running personal agents reachable through messaging surfaces. Sources: Hermes docs (`https://hermes-agent.nousresearch.com/docs/`, `https://hermes-agent.nousresearch.com/docs/user-guide/features/tools`, `https://hermes-agent.nousresearch.com/docs/user-guide/features/cron/`).

## What The User Probably Wants

### 1. Specific Proactive Value, Not Generic Companion Nudges

The user likely wants messages like:

- "wiki brief landed: 3 new notes touched ai, tools, memory. want the 3-line version?"
- "inbox has 5 personal unread, 31 promo/update. read personal or nuke promos?"
- "calendar changed since yesterday: new 14:30 call. want prep?"
- "you have an unresolved prediction due today. mark it right/wrong?"
- "that task you left open is still sitting there. keep, drop, or schedule?"

The core principle: every proactive message should name its source and offer a next action.

### 2. Source-Level Control

The app should let the user tune proactive behavior without editing YAML:

- `/proactive status`
- `/proactive off reengage_silence`
- `/proactive on wiki_new_file`
- `/proactive snooze gmail 3d`
- `/proactive quiet today`
- `/proactive why`

This preserves the character while giving the owner control over annoyance.

### 3. Memory As A Visible Object

The user likely wants Hikari to remember, but also wants to inspect and repair the memory model:

- `/memory search <topic>`
- `/memory recent`
- `/memory what_about_me`
- `/memory correct <id> <new fact>`
- `/memory forget <id>`
- `/memory pin <id>`
- `/memory why`

Best first version: after a memory write, send a tiny confirmation with a `forget` button or a short ID. Example: `remembered: prefers terse UX reviews [forget]`.

### 4. Trust And Tool Transparency

The agent should expose what it did without making the chat feel like logs:

- `/approvals` for pending approval gates
- `/tools recent` for recent reads/writes/sends/deletes
- `/integrations` for Gmail, Calendar, Drive, Apple Notes, Apple Reminders, wiki, and Telegram health
- `/cost` already exists; it should be linked from `/status`

For risky operations, typed confirmation can remain, but the message should include buttons for `cancel`, `details`, and maybe `allow this session` where safe.

### 5. A Telegram-First Control Surface

Before building a full dashboard, add native Telegram affordances:

- bot command menu with high-signal commands
- inline keyboards for daily check-in, reminders, approvals, and proactive pings
- callbacks for "useful", "too much", "wrong", "snooze", "show source"
- one `/status` message with buttons into memory, proactive, integrations, approvals, and cost

This keeps the product where the user already talks to Hikari.

## Priority Recommendations

### P0 - Make Existing Capability Discoverable

Add Telegram command registration via `set_my_commands` or python-telegram-bot's equivalent startup hook.

Suggested command set:

- `/help` - compact capability menu
- `/status` - alive, silence state, proactive counts, pending approvals, failed integrations, cost
- `/proactive` - source controls and recent proactive events
- `/memory` - search/recent/correct/forget
- `/approvals` - pending and recent approval outcomes
- `/tools` - recent tool calls and connector health
- keep existing `/tasks`, `/cancel`, `/cost`, `/silence`, `/unsilence`

Do not expose debug-only commands like `/memory_diff` as a primary user command unless it is clearly labeled.

### P0 - Add Inline Buttons For The Flows That Already Exist

Start with buttons where the backend already has a deterministic next step:

- Daily check-in: `Email`, `Calendar`, `Both`, `Skip`
- Email digest: `Read personal`, `Nuke promos`, `Later`
- Calendar digest: `Prep`, `Ignore`
- Proactive ping: `Show`, `Snooze source`, `Quiet today`, `Wrong`
- Reminder: `Done`, `Snooze 1h`, `Tomorrow`, `Drop`
- Approval: `Details`, `Cancel`; keep typed `CONFIRM-SEND` for high-risk send/delete until the trust model is hardened

This reduces ambiguity and makes the app feel intentional rather than parser-driven.

### P1 - Add Source-Level Proactive Settings

Use the existing source tags and cadence pools as the model. Persist per-source state:

- enabled/disabled
- snoozed_until
- last_sent_at
- last_feedback
- fire count in 24h/7d

Then make `/proactive status` a readable summary:

```text
proactive:
on: calendar_event, wiki_new_file, open_loop, daily_checkin
snoozed: gmail_unread until monday
off: reengage_silence
last 7d: 6 sent, 3 useful, 1 too much
```

### P1 - Add Memory Ledger UX

Memory should become a managed surface:

- show saved facts with IDs
- show source/date where possible
- support correct/forget from chat
- show "top of mind" facts separately from long-tail facts
- after important memory writes, let the user undo immediately

This mirrors the memory controls users now expect from modern assistant products.

### P1 - Add "Why Did You Say This?" For Proactive And Memory-Sourced Messages

When a message uses a proactive source, callback candidate, or remembered fact, store a compact explanation payload. Then `/why` after the latest message can answer:

```text
i said that because:
- source: calendar_event
- trigger: event starts in 58m
- memory used: you prefer terse prep before calls
- action available: prep / snooze calendar
```

This is one of the highest-trust features for an agent that acts over time.

### P1 - Add Integration Health

Create `/integrations` or include it in `/status`:

- Gmail: ok / auth missing / last checked
- Calendar: ok / scope missing / last checked
- Drive: ok / unavailable
- Apple Reminders: ok / permission denied
- Apple Notes: ok / permission denied
- wiki: ok / path missing / last indexed
- Telegram: ok / last send failed

The user is technical. They will appreciate concrete failure states more than vague apologies.

### P2 - Build A Small Cockpit If Telegram Becomes Too Dense

If commands/buttons become crowded, build a simple cockpit, preferably as a Telegram Mini App or local web page:

- Today: open loops, due reminders, calendar, unread personal mail
- Memory: recent/pinned/corrections
- Proactive: enabled sources, fire history, feedback
- Approvals: pending and recent
- Tools: connector health and recent activity
- Cost: daily/weekly spend

Keep it utilitarian. No landing page, no decorative hero. The goal is operator clarity.

## Suggested First Sprint

1. Register command menu and implement `/help`.
2. Implement `/status` with silence state, pending approvals, proactive counts, integration health, and cost pointer.
3. Add inline keyboard plumbing and callback query router.
4. Convert daily check-in yes/no to buttons.
5. Add `/proactive status` and source snooze/disable.
6. Add `/memory recent` plus "forget this" for new memory confirmations.
7. Add `/approvals` for pending and recent approval gates.

This sprint would make the app feel much more usable without changing the model, persona, or core architecture.

## Product Bet

Hikari should not become a generic assistant dashboard. The product shape should be:

- Telegram chat for personality and high-context interaction
- Telegram buttons for fast decisions
- small status/cockpit surface for trust, repair, and control
- proactive messages only when they are grounded, named, and actionable

That is the UX gap: the agent is already alive in the backend, but the user needs handles to steer it.
