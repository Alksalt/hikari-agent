# Second-Pass Review: Product Capabilities / Workflow

Date: 2026-05-24
Repository: `/Users/ol/agents/hikari-agent`
Scope: current working tree as-is. Prior `codex/*.md` files were treated as context only, not truth.

## 1. Current-State Summary

The product surface is no longer just hidden backend machinery. The current tree has real user-facing workflow leverage in these areas:

- Cockpit/status: `/help`, `/status`, `/tools`, `/audit`, `/settings`, `/memory`, `/approvals`, and `/proactive` are implemented command surfaces, with tests covering key paths (`agents/telegram_bridge.py:2249`, `agents/cockpit.py:240`, `tests/test_telegram_cockpit_cmds.py`, `tests/test_telegram_memory_cmd.py`).
- Memory/session review: `/memory recent/search/fact/forget/correct/tasks/session/why/debug` exists and is tested, and session search is backed by FTS (`agents/telegram_bridge.py:1495`, `tools/memory/session_search.py`, `tests/test_telegram_memory_cmd.py`).
- Link shelf: save/search/list/update/delete exist, exact duplicate URLs update in place, metadata fetches have SSRF protections, and untrusted page text is wrapped before model exposure (`tools/link_shelf/handlers.py:7`, `tools/link_shelf/_safe_fetch.py:164`, `tests/test_link_shelf.py`, `tests/test_link_shelf_ssrf.py`).
- Reminders: create/list/cancel/snooze plus dedicated due-reminder firing exist, with repeat handling and Google/Apple sync hooks (`tools/reminders/create.py:1`, `agents/proactive.py:193`, `tests/test_reminders_tool.py`, `tests/test_reminders_scheduler.py`).
- Day receipts: `receipt_add`, `receipt_today`, `receipt_get`, `receipt_print`, `receipt_week`, `receipt_search`, `receipt_set_note`, and `receipt_delete` exist, and `CLAUDE.md` teaches Hikari when to use them (`CLAUDE.md:123`, `tools/day_receipt/README.md:21`, `tests/test_day_receipt.py`).
- Daily check-in and morning brief: there is a real daily inbox/calendar check-in and a real morning weather brief (`agents/daily_checkin.py:636`, `agents/morning_brief.py:1`, `tests/test_daily_checkin_orchestrator.py`, `tests/test_morning_brief.py`).
- Coding workflow: background dispatch, `/tasks`, `/cancel`, and read-only Codex reports exist (`tools/dispatch/_shared.py:37`, `agents/telegram_bridge.py:1390`, `tools/codex/list_reports.py`, `tests/test_codex.py`).

The main caution is that some "unified proactive" sources now overlap with older authoritative jobs. That turns otherwise useful workflow capabilities into possible duplicate pushes. Also, several roadmap-grade surfaces are still stubs or chat-only affordances: Readwise/Reader, shift-aware scheduling, structured wiki filing, bot command menus/buttons, and complete tool telemetry in the cockpit.

Focused verification run:

```text
uv run python -m pytest tests/test_telegram_cockpit_cmds.py tests/test_telegram_memory_cmd.py tests/test_link_shelf.py tests/test_link_shelf_ssrf.py tests/test_day_receipt.py tests/test_reminders_tool.py tests/test_reminders_scheduler.py tests/test_phase_i_proactive.py tests/test_daily_checkin_orchestrator.py tests/test_daily_checkin_schedule.py tests/test_morning_brief.py -q

217 passed, 1 warning in 2.73s
```

## 2. Findings

### P0

None found in this pass.

### P1

#### P1-1. Due reminders can be surfaced through two outbound paths

`reminder_fire` is enabled in the default proactive source list (`config/engagement.yaml:36`, `config/engagement.yaml:39`, `config/engagement.yaml:898`). The dedicated reminder job also drains due reminders and marks them fired (`agents/proactive.py:193`). The engagement producer explicitly says it does not fire reminders itself and only exposes them to the unified selector (`agents/engagement/producers/reminder_fire.py:1`), but it emits a sendable `TriggerCandidate` with source `reminder_fire` and dedup key `reminder_fire:{id}` (`agents/engagement/producers/reminder_fire.py:39`, `agents/engagement/producers/reminder_fire.py:46`).

The authoritative sender uses source `reminder` and dedup key `reminder:{id}` (`agents/proactive.py:216`). Because dedupe is source/key based, the same due reminder can be sent as a literal reminder by the dedicated job and as an engagement-composed proactive candidate by `engagement_tick`. The engagement path also does not mark the reminder fired.

Impact: a core user-facing capability, reminders, can become duplicate/noisy. This is workflow leverage only if it is exact and trustworthy.

Recommendation: make `fire_due_reminders` the only outbound path for due reminders. If `reminder_fire` remains in engagement, it should be dashboard/context-only, or it must call the same authoritative mark-fired/reschedule path and use the same producer/dedup identity.

#### P1-2. Decision follow-ups can be asked repeatedly or through competing paths

`decision_resolve_due` is enabled by default (`config/engagement.yaml:41`, `config/engagement.yaml:900`) while the older weekly decision resolver still exists (`agents/scheduler.py:176`, `agents/decision_log.py:18`). The weekly resolver sends a question and marks `asked_at` (`agents/decision_log.py:61`, `agents/decision_log.py:65`). The engagement producer emits a candidate (`agents/engagement/producers/decision_resolve_due.py:34`) but does not update `asked_at` or otherwise mark the decision consumed.

Impact: unresolved decisions are good proactive material, but the current split can create repeated asks after the dedupe window, or duplicate asks between the weekly resolver and engagement tick.

Recommendation: choose one owner. Either disable the engagement producer by default, or make the engagement sender call the same `db.decision_mark_asked` path after a successful send.

### P2

#### P2-1. Readwise/Reader is a visible roadmap surface, not an implemented workflow

The current tree contains a `readwise_daily_review` producer, but it is intentionally a stub that always returns no candidates because the prior Readwise MCP was removed (`agents/engagement/producers/readwise_daily_review.py:1`). The config keeps the source present but disabled (`config/engagement.yaml:916`), and no local Readwise tools or `READWISE_TOKEN` configuration were found in the current README/config search.

This is not user-facing workflow leverage yet. It is mostly product intent. The official Reader API supports saving documents and listing documents via token-authenticated HTTP endpoints, including `POST /api/v3/save/`, `GET /api/v3/list/`, locations such as `new/later/archive/feed`, and rate limits documented by Readwise. That makes a direct API MVP feasible without waiting for another MCP server.

Recommendation: treat Readwise/Reader as a high-leverage next pipeline only when connected to concrete actions: save link to Reader, review Reader inbox/later queue, file highlights/notes into wiki, and optionally add a daily review source.

#### P2-2. `/tools recent` and `/audit tools` omit most utility-tool telemetry

Utility tools are instrumented to write every call to `tool_calls` (`tools/_telemetry.py:1`, `tools/_telemetry.py:42`, `tools/_registry.py:151`, `storage/db.py:766`, `storage/db.py:2621`). Cockpit tool/audit commands, however, read `audit_log` via `audit_recent` and `audit_tool_counts_7d` (`agents/cockpit.py:344`, `agents/cockpit.py:396`, `agents/cockpit.py:411`, `storage/db.py:3124`, `storage/db.py:3159`).

Impact: `/tools recent` can say "no tool calls in audit log yet" even after normal utility calls such as reminders, link shelf, receipts, weather, or arxiv. That weakens the exact status/audit workflow the prior reviews correctly ranked as high leverage.

Recommendation: add cockpit views over `tool_calls`, and reserve `audit_log` wording for approval/hash-chain events. Ideally `/tools recent` should show both ordinary telemetry and gated/audited calls clearly.

#### P2-3. `/cancel` wording overpromises cancellation semantics

The README says `/cancel` will "cancel a pending in-flight tool call" (`README.md:339`). The implementation requires a task id prefix and marks a background task row cancelled (`agents/telegram_bridge.py:1422`), while the dispatch worker documentation/flow records task state and final output after execution (`tools/dispatch/_shared.py:153`). I did not find evidence that nested Claude SDK/tool execution is interrupted immediately.

Impact: this is a trust issue for the coding workflow. The user may believe an autonomous task stopped when it may continue until its current run yields or hits a limit.

Recommendation: update wording to "mark a background task cancelled / prevent follow-up delivery" unless true process cancellation is added. If true cancel is desired, add an interrupt mechanism and a test that proves the worker stops mid-run.

#### P2-4. Cockpit commands exist, but Telegram discovery/actions are still text-only

The command handlers are real (`agents/telegram_bridge.py:2249`), but repository search did not find bot command menu setup or inline-button/callback flows (`set_my_commands`, `InlineKeyboard`, `CallbackQuery`, `MenuButton`, or `WebApp`). Telegram's official bot platform supports command menus and inline keyboards, so this is an available product affordance, not a platform limitation.

Impact: cockpit controls are now real, but still rely on the user remembering slash commands and text subcommands. For workflow surfaces like approvals, reminders, daily check-in, proactive snooze, and memory corrections, buttons would convert hidden tool power into lower-friction leverage.

Recommendation: add a minimal bot command menu plus inline buttons for the highest-repeat actions: approve/defer/deny, snooze source, mark reminder done/cancel, answer daily check-in yes/no, save link to shelf/Reader/wiki.

#### P2-5. Wiki filing is still append/read/search, not durable filing workflow

The wiki tools correctly use `python-frontmatter` and preserve frontmatter on append (`tools/wiki/_shared.py:4`, `tools/wiki/_shared.py:183`, `tools/wiki/_shared.py:206`). But the append path mainly resolves a note, optionally appends under an H2, and writes it back (`tools/wiki/append.py:20`, `tools/wiki/_shared.py:157`). I did not find helper logic that enforces the vault filing conventions from `AGENTS.md`: update `updated:`, maintain `index.md`, append operations to `log.md`, protect `raw/`, or turn link/Reader material into structured notes.

Impact: wiki tools are useful but not yet a workflow. They help Hikari write into a page; they do not yet make capture-to-knowledge reliable.

Recommendation: add `wiki_file`/`wiki_ingest_link` style helpers with convention checks and tests. Link shelf and Reader should feed this rather than asking the model to hand-edit markdown conventions each time.

#### P2-6. Shift-aware workflow is not implemented

The daily check-in design doc explicitly calls for a configurable, shift-aware routine (`docs/superpowers/specs/2026-05-20-daily-inbox-calendar-routine-design.md:10`, `docs/superpowers/specs/2026-05-20-daily-inbox-calendar-routine-design.md:45`). Current code supports a default/override/skip schedule stored in `core_blocks.daily_checkin_schedule` (`agents/daily_checkin.py:45`, `agents/daily_checkin.py:68`, `agents/daily_checkin.py:135`), but I found no shift import, shift ledger, or shift-aware target-time logic.

Impact: the daily check-in is real, but "shifts" remain roadmap. Given the user's variable schedule, this is likely true workflow leverage, not decorative automation.

Recommendation: implement a small shift ledger/importer before broadening briefings. Use it to drive check-in time, reminder defaults, and proactive quiet windows.

#### P2-7. `/status` is useful but narrower than the startup health/audit story

`/status` reports uptime, silence state, scheduler jobs, MCP warm pool state, Google OAuth keychain state, DB counts, cost, proactive sends, and graph outbox (`agents/cockpit.py:240`). README startup health lists a richer boot digest: DB integrity, scheduler jobs, MCP warm pool, OAuth, graph outbox, media outbox, backup age, and recent log errors (`README.md:247`).

Impact: `/status` is now useful workflow leverage, but it is not yet the single "what is Hikari doing and is she healthy?" surface. It misses some of the operational signals a user would expect from the README/startup health story.

Recommendation: either align `/status` with startup health fields, or split into `/status` and `/status health` with the richer health report.

### P3

#### P3-1. Briefings are fragmented rather than source-driven

Morning weather brief is implemented (`agents/morning_brief.py:1`). `arxiv_search` is available as an on-demand utility (`tools/arxiv_search/search.py:1`). Daily check-in handles inbox/calendar (`agents/daily_checkin.py:636`). But there is no source registry for repeatable briefings covering Reader, RSS/HN, arxiv, wiki deltas, links, calendar, and project state with a common "run now / configure / send summary" interface.

Impact: individual brief pieces are useful, but "briefings" as a product workflow are still scattered.

Recommendation: after fixing duplicate proactive sends, define a small `brief_sources` registry and one user command/surface to run, preview, and schedule briefs.

#### P3-2. Link shelf is strong but not connected to Reader/wiki/project workflows

The link shelf itself is solid: SSRF-safe metadata fetch, exact duplicate handling, search, update, delete, and tags are present (`tools/link_shelf/handlers.py:140`, `tools/link_shelf/_safe_fetch.py:164`, `tests/test_link_shelf.py`). The missing piece is workflow connection. I did not find actions like "send saved link to Reader", "file this link into wiki", "attach link to project", or "summarize unread saved links".

Impact: link shelf is already leverage for capture and resurfacing; it becomes much more valuable if it is the intake layer for Reader/wiki/project memory.

#### P3-3. Day receipts are implemented, but not yet surfaced as a cockpit habit

Day receipt tools are complete enough for chat-triggered use (`CLAUDE.md:123`, `tools/day_receipt/README.md:21`). There is no `/receipt` Telegram command or status surface that summarizes today's open receipt/habits.

Impact: not a blocker. This is already a low-friction conversational workflow, but a tiny command could make it more discoverable.

#### P3-4. Coding workflow exists but is thin on observability

Dispatch sessions and `/tasks` give a usable start (`tools/dispatch/_shared.py:37`, `agents/telegram_bridge.py:1390`), and static Codex report tools exist (`tools/codex/list_reports.py`). The missing product layer is artifact/tail visibility: last logs, changed files, report links, retry, and truthful cancellation status.

Impact: coding workflow is real leverage for supervised async work, but it needs better cockpit affordances before it is safe to rely on for longer tasks.

## 3. Previously Reported Issues That Now Look Closed

- Cockpit commands are no longer just a recommendation. `/help`, `/status`, `/tools`, `/audit`, `/settings`, `/memory`, and `/proactive` are registered and tested (`agents/telegram_bridge.py:2249`, `tests/test_telegram_cockpit_cmds.py`, `tests/test_telegram_memory_cmd.py`).
- Memory/session inspection is now real, including `/memory session` and `/memory why` style surfaces (`agents/telegram_bridge.py:1495`, `tools/memory/session_search.py`).
- Link shelf hardening looks materially improved: SSRF-safe fetch, duplicate update-in-place, metadata wrapping, and tests are present (`tools/link_shelf/_safe_fetch.py:164`, `tests/test_link_shelf_ssrf.py`).
- Day receipt exists as a concrete utility and Hikari instruction path (`CLAUDE.md:123`, `tools/day_receipt/README.md:21`, `tests/test_day_receipt.py`).
- Reminder CRUD/scheduler behavior is implemented and tested (`tools/reminders/create.py:57`, `agents/proactive.py:193`, `tests/test_reminders_scheduler.py`).
- Startup health exists as a boot-time operational report (`agents/health.py`, `tests/test_health.py`).
- Proactive controls are real: `/proactive status/recent/why/snooze` exists, and selector code respects source snoozes (`agents/cockpit.py:528`, `agents/engagement/selector.py`).

## 4. New Regressions Or Contradictions

- The unified engagement path appears to have introduced duplicate-capable outbound paths for reminders and decision follow-ups. This is the most important new workflow risk.
- README says `/cancel` cancels an in-flight tool call, but implementation appears to mark background task state rather than interrupt execution (`README.md:339`, `agents/telegram_bridge.py:1412`).
- `/tools recent` and `/audit tools` are framed as tool activity views but query `audit_log`, while ordinary utility calls are recorded in `tool_calls` (`agents/cockpit.py:344`, `tools/_telemetry.py:1`).
- `readwise_daily_review` exists in the producer/composer/config vocabulary but is disabled and intentionally empty (`agents/engagement/producers/readwise_daily_review.py:1`, `config/engagement.yaml:916`).
- The daily check-in spec says shift-aware, but the current implementation is schedule-aware only (`docs/superpowers/specs/2026-05-20-daily-inbox-calendar-routine-design.md:10`, `agents/daily_checkin.py:45`).

## 5. Missing Tests / Suggested Verification

- Add an integration test proving a due reminder is sent exactly once when both the dedicated reminder scheduler and `engagement_tick` run near the same timestamp.
- Add a decision follow-up test proving that a successful engagement send either marks `asked_at` or cannot duplicate the weekly decision resolver.
- Add cockpit tests showing `/tools recent` includes ordinary `tool_calls` telemetry, not only `audit_log`.
- Add a README/behavior test or unit test around `/cancel` semantics once product wording is corrected or true cancellation is implemented.
- Add tests for bot command menu registration and inline callback handlers if the product chooses Telegram buttons.
- Add wiki filing tests that verify `updated:` maintenance, index update, log append, and `raw/` protection once a filing helper is added.
- Add shift ledger/import tests before wiring shifts into check-ins and proactive quiet windows.
- Add mocked Readwise API tests for token validation, save, list with pagination, updated-after sync, and rate-limit/backoff handling.

## 6. Sprint Or Roadmap Implications

Capabilities that are truly user-facing workflow leverage:

- Reminders, day receipts, link shelf, memory/session search, daily check-in, morning weather brief, proactive controls, status/audit cockpit, and coding dispatch are real because they map to repeatable user jobs.
- Readwise/Reader is likely high leverage, but only after it becomes a real link/reading/wiki pipeline.
- Shift-aware scheduling is high leverage for this user because it would change when the assistant speaks, checks inbox/calendar, and reminds.
- Wiki filing is high leverage if implemented as a convention-enforcing workflow, not just append-to-markdown.

Capabilities that currently risk being tool clutter:

- Stub or disabled proactive sources that appear in config/composer without data behind them, especially `readwise_daily_review`.
- Default-enabled proactive producers that overlap authoritative jobs, especially `reminder_fire` and `decision_resolve_due`.
- Large slash-command/tool lists without command menu/buttons or clear next actions.
- Audit/status views that look complete but omit key telemetry tables.
- Generic "briefing" ambitions without a source registry and user controls.

Suggested next sprint order:

1. Fix duplicate reminder/decision proactive paths before adding more proactive sources.
2. Merge `tool_calls` into `/tools recent` and `/audit tools`, or rename those surfaces so they are honest.
3. Add Telegram command menu plus minimal inline buttons for approvals, reminders, proactive snooze, daily check-in, and link capture.
4. Implement direct Readwise/Reader API MVP: token check, save URL, list updated docs, daily review candidate, and link shelf integration.
5. Add a wiki filing helper that enforces vault conventions and accepts link/Reader/project inputs.
6. Add shift ledger/import and wire it into daily check-in/proactive quiet windows.
7. Improve coding workflow cockpit: true cancel semantics or honest cancel wording, task log tail, artifact/report links, retry.

## 7. Sources Used

Local priors read as context only:

- `codex/other-tools-review-2026-05-23.md` from `HEAD`
- `codex/tool-priority-correction-2026-05-23.md` from `HEAD`
- `codex/ux-review-what-user-wants-2026-05-23.md` from `HEAD`

Current local source, tests, config, and docs:

- `agents/telegram_bridge.py`
- `agents/cockpit.py`
- `agents/scheduler.py`
- `agents/proactive.py`
- `agents/daily_checkin.py`
- `agents/morning_brief.py`
- `agents/decision_log.py`
- `agents/engagement/**`
- `tools/link_shelf/**`
- `tools/wiki/**`
- `tools/day_receipt/**`
- `tools/reminders/**`
- `tools/dispatch/**`
- `tools/codex/**`
- `tools/memory/session_search.py`
- `tools/_telemetry.py`
- `tools/_registry.py`
- `storage/db.py`
- `config/engagement.yaml`
- `config/tools.yaml`
- `README.md`
- `CLAUDE.md`
- `docs/superpowers/specs/2026-05-20-daily-inbox-calendar-routine-design.md`
- Focused tests listed in the verification command above.

External primary/official sources:

- Readwise Reader API: https://readwise.io/reader_api
- Telegram Bot Features, commands and inline keyboards: https://core.telegram.org/bots/features
