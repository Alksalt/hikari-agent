# Daily inbox + calendar check-in routine

**Status**: design approved, awaiting implementation plan
**Date**: 2026-05-20
**Owner**: Aleksandr (single-user bot)

## Goal

Give Hikari a once-per-day check-in routine where she asks the user — at a
configurable, shift-aware time — whether to check email and/or calendar. If
yes, she fetches a structured summary, presents it in voice, and (for email)
proposes a delete sweep of promotional/update categories. Deletions run
through the existing `CONFIRM-SEND` destructive-tool gate; no new approval
infrastructure is needed.

Out of scope: replying to emails, moving emails between labels, modifying
calendar events. Those keep their existing on-demand paths via the
`drive_gmail` subagent.

## User experience

1. At the configured time, Hikari posts one short message asking two yes/no
   questions in one go: "morning. check emails? check calendar? yes or no,
   separately."
2. User answers in natural language. The bot parses intent (regex first,
   LLM fallback for ambiguous phrasing) into per-topic booleans
   `{email: bool, calendar: bool}`.
3. For each `true`:
   - **email** → fetch unread inbox, calendar invites, and a deletable
     pile (promos + updates last 7d). Compose one voice message that
     names personal-bucket subjects, summarises the deletable count + top
     senders, and ends with a proposal: *"want me to nuke the 28 promos?"*
   - **calendar** → fetch today's events. Compose one voice message with
     first event, imminent events (next 2h), and new-since-yesterday
     calls-out.
4. If the user accepts the email-delete proposal with an affirmative reply,
   Hikari calls `mcp__google_workspace__gmail_bulk_delete_messages`. The
   existing `defer_gated_tools` PreToolUse hook fires, sends the
   `⏸️ delete N promotional emails... type CONFIRM-SEND` prompt, halts
   the SDK. On `CONFIRM-SEND`, `tools/approvals._resume_after_defer` runs
   the confirmed-tool sibling and audits.

## Schedule model

The routine does not use a fixed cron. The user has variable shifts. A new
APScheduler `IntervalTrigger(minutes=5)` job (`daily_checkin`) polls every
5 minutes, reads the schedule state, and fires when local clock matches
today's target time AND the routine has not already fired today.

State lives in `core_blocks.daily_checkin_schedule` as a small YAML payload:

```yaml
default_time: "07:00"          # local clock, used absent overrides
override_date: "2026-05-21"     # one-shot override; cleared after firing
override_time: "06:30"
skip_dates: ["2026-05-22"]      # one-day skips; cleared once past
```

Last-fired tracking lives in `runtime_state.daily_checkin_last_fired_date`
as a local ISO date string — guards against the 5-minute poll firing twice
within the same day.

### Natural-language schedule edits

Parsed in the user-turn path (post-`respond`, pre-routing). If no pattern
matches, the message routes normally — no false positives stealing real
chat. Patterns:

- `check in at HH:MM tomorrow` → set `override_date=tomorrow,
  override_time=HH:MM`
- `from now on check in at HH:MM` / `set morning check to HH:MM` → mutate
  `default_time`
- `skip the morning check tomorrow` → append tomorrow's date to
  `skip_dates`
- `what time is my check-in?` → read the block and answer

Uses `dateparser` (already a dependency via `tools/reminders.py`) for
relative-date resolution.

## Email fetch contract

The `daily_checkin` job delegates to `drive_gmail` with a single structured
prompt that asks for three buckets in one fetch. Returned as strict YAML,
parsed defensively (`yaml.safe_load` inside `try/except yaml.YAMLError`;
failure = swallow + log warning, no leak):

```yaml
unread_personal:        # is:unread is:inbox, not category:promotions, not category:updates
  - {id, from, subject, snippet}
calendar_invites:       # has:invite OR from contains "noreply@calendar.google.com"
  - {id, from, subject}
deletable:              # category:promotions OR category:updates, last 7d
  count: 28
  top_senders: ["linkedin.com", "spotify.com", "uber.com"]
  sample_ids: [...]     # ALL IDs in the bucket, capped at 200
```

Defensive contract: any failure (auth 401, MCP unreachable, malformed
output) returns the empty result `{unread_personal: [], calendar_invites:
[], deletable: {count: 0, top_senders: [], sample_ids: []}}`. The voice
layer skips the email portion silently in that case.

## Calendar fetch contract

Single delegation to `drive_gmail`:

```yaml
events:
  - {id, title, start_iso, end_iso, location, attendees_count, is_new_since_yesterday}
```

Window: now → end of local day, primary calendar.

`is_new_since_yesterday` is computed locally by diffing the returned IDs
against `runtime_state.calendar_last_known_event_ids` (a JSON list, updated
each fire). The boolean lets Hikari call out additions without re-querying.

Same defensive YAML parsing as the email fetch.

**No deletion proposal in the calendar path.** If the user wants to delete
an event, they ask explicitly ("kill the 16:30") on a separate turn, which
triggers `mcp__google_workspace__delete_calendar_event` via the existing
gate (`config/engagement.yaml:65`). The check-in itself never proposes
calendar mutations.

## Voice composition

**One call per active topic, not a single combined message.** If the user
said yes to both, the routine fires `run_visible_proactive` twice (email
message first, then calendar message), back-to-back. This keeps each
message short (Hikari's voice rule: 1-4 sentences) and lets the email-side
delete proposal stand on its own without being buried under calendar
prose.

Each call uses a prompt that includes:

- the parsed structured data for that topic,
- per-topic templates ("personal-bucket subjects cap 5", "deletable
  proposal only if count > 0", etc.),
- a `NO_MESSAGE` escape hatch.

The returned text is checked against two new guards before being sent to
`send_text`:

1. **SDK-error-text guard**: if the text starts with `Failed to
   authenticate` / `API Error:` / `401 ` (case-insensitive), treat as
   failure, skip, log warning. This is a belt-and-suspenders fix for the
   already-observed 401 leak (see `agents/proactive.py` heartbeat / reengage
   on 2026-05-20).
2. **Empty / NO_MESSAGE guard**: existing pattern, kept consistent with
   other proactive paths.

If either guard trips, the routine logs and skips — never sends.

## Deletion flow

User affirmative ("yeah", "go", "do it") to the email delete proposal
triggers Hikari to call:

```
mcp__google_workspace__gmail_bulk_delete_messages(
    message_ids=deletable.sample_ids[: cfg.daily_checkin.max_delete_ids]
)
```

`config/engagement.yaml` already registers this tool in
`approvals.defer_gated_tools` (line 64). The PreToolUse hook fires
automatically:

1. Persists a deferred-approval row,
2. Sends the `⏸️ delete N promotional emails... type CONFIRM-SEND` prompt
   to the owner,
3. Halts the SDK.

On `CONFIRM-SEND`, `_resume_after_defer` (in `tools/approvals.py:233`)
runs the confirmed-tool sibling and writes the audit row.

**Safety:**
- Hard cap on IDs per call: `daily_checkin.max_delete_ids` (default 200).
- Personal-bucket emails are **never** in the delete proposal. Only
  `category:promotions` and `category:updates` are eligible.
- The defer-prompt summary shows count + top 3 sender domains so the user
  has signal before typing CONFIRM-SEND.

## Intent parsing

Cheap regex first, LLM fallback only on ambiguity. All matches are
case-insensitive, leading-whitespace tolerant, and treat short replies
holistically:

- **affirmative**: `^(y|yes|yeah|yep|ok|okay|go|do it|sure|fine)\b`
- **negative**: `^(n|no|nope|nah|skip|leave (it|them)|not now)\b`
- **selective**: `^(just|only) (email|emails|inbox|calendar|cal)\b` →
  `{email: True/False, calendar: True/False}` accordingly
- **both**: `^(both|yes both|both yes)\b` → both true
- **skip-day**: parsed via `dateparser`, mutates `skip_dates`

Anything that doesn't match any pattern triggers one `run_internal_control`
fallback call asking: *"did the user agree to: emails? calendar? both?
neither? respond as `email=y/n, calendar=y/n`."* Fallback cost is
~$0.005 per ambiguous reply; only fires when needed.

## Code layout

**New files:**
- `agents/daily_checkin.py` — main module. Mirrors `agents/morning_brief.py`
  shape. Exports:
  - `maybe_run_daily_checkin(send_text)` — scheduler job entry
  - `parse_schedule_edit(text)` — returns `Optional[ScheduleEdit]` for the
    bridge's user-turn pre-router
  - `parse_intent(text)` — returns `{email: bool, calendar: bool}` or
    `None` for ambiguous
  - `_fetch_email_buckets()`, `_fetch_calendar_events()`,
    `_compose_message()` — private helpers
- `tests/test_daily_checkin.py` — unit coverage on:
  - schedule resolver under default / override / skip / already-fired-today
  - regex intent parser table-driven cases
  - YAML-defensive fetches (auth-error string, malformed YAML, missing keys)
  - SDK-error-text guard in the voice composition path
  - dedup guard against double-firing on the 5-min poll

**Edits:**
- `agents/scheduler.py` — register `_daily_checkin_job` on
  `IntervalTrigger(minutes=5)`, similar to existing job wiring at line 39.
- `agents/telegram_bridge.py` — in the user-turn dispatch, before calling
  `respond()`, run `daily_checkin.parse_schedule_edit(text)`; if it matches,
  apply the schedule mutation, reply with a short acknowledgement, and
  short-circuit. (Same pattern as the existing approval-resume short-circuit
  at the top of the inbound message handler.)
- `config/engagement.yaml` — new section:

  ```yaml
  daily_checkin:
    enabled: true
    default_time: "07:00"
    max_delete_ids: 200
    personal_subject_cap: 5
    deletable_top_senders_cap: 3
    poll_interval_minutes: 5
  ```

**No schema migration:** all state keys live in existing `core_blocks`
and `runtime_state` tables.

## Error handling summary

| failure mode                                    | behavior                                                |
|-------------------------------------------------|---------------------------------------------------------|
| drive_gmail returns 401 / auth error            | YAML parse fails → skip topic, log warning              |
| drive_gmail returns malformed YAML              | parse fails → skip topic, log warning                   |
| voice composition returns SDK error string      | error-text guard catches → skip send, log warning       |
| voice composition returns empty / NO_MESSAGE    | existing skip pattern                                   |
| user gives ambiguous reply to check-in question | LLM fallback parser disambiguates; if still unclear,    |
|                                                 | Hikari asks one clarifying question                     |
| `gmail_bulk_delete_messages` call fails post-CONFIRM-SEND | existing `_resume_after_defer` audit path; user notified |
| Calendar API down but user wants email only     | email path runs; calendar path skips silently           |
| `daily_checkin.enabled = false` in config       | scheduler job not registered                            |

## Cost model

Per check-in (full both-yes path):

- intent regex parse: free
- drive_gmail email fetch (haiku, structured): ~$0.02
- drive_gmail calendar fetch (haiku, structured): ~$0.01
- voice composition for email (visible proactive, opus): ~$0.02
- voice composition for calendar (visible proactive, opus): ~$0.02
- **total per check-in (both topics yes)**: ~$0.07
- **total when only one topic yes**: ~$0.04

Daily cap: 1 check-in per day. Negligible compared to baseline session
cost.

## Cadence governor integration

The check-in counts as a proactive event for cadence purposes:

- `agents/cadence.py:can_send_proactive("daily_checkin")` is called before
  the trigger fires.
- On send, `cadence.record_proactive_sent()` is called so the proactive cap
  heuristic accounts for it.

A new `daily_checkin` source key is added to whatever allowlist
`cadence.can_send_proactive` consults (existing pattern, no code structure
change).

## Open questions

None at design time. Implementation may surface edges around dateparser
locale handling or the regex priorities; those are plan-time decisions.

## Related context

- Existing 401 leak: heartbeat at 15:15:43 on 2026-05-20 shipped
  `Failed to authenticate. API Error: 401...` as the message body. The
  SDK-error-text guard in this design is a forward-fix; the parallel
  fix in `agents/runtime.py:run_visible_proactive` to raise on error
  text is a separate small change tracked outside this spec.
- Existing destructive-tool gate: `config/engagement.yaml:60`
  (`approvals.defer_gated_tools`).
- Existing morning brief at 06:00 (`agents/morning_brief.py`) is
  unchanged.
