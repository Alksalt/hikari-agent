---
title: Second-Pass Telegram UX / Operator Cockpit Review
date: 2026-05-24
repo: /Users/ol/agents/hikari-agent
domain: telegram-ux-operator-cockpit
tags:
  - codex
  - second-pass
  - telegram
  - ux
  - operator-control
  - observability
---

# Second-Pass Telegram UX / Operator Cockpit Review

## 1. Current-state summary

Answer to the core question: **partly, but not yet enough to fully trust the agent from Telegram without SSH.**

Current Telegram UX is materially better than the 2026-05-23 priors described. The repo now has a text-first operator cockpit:

- `/help`, `/status`, `/tools`, `/audit`, `/settings`, `/memory`, `/approvals`, and `/proactive` are implemented and registered in `agents/telegram_bridge.py`.
- `/memory` is no longer only a debug diff; it supports recent/search/fact/forget/correct/session/why/debug.
- `/proactive` supports source status, on/off, recent, why, and snooze, and the engagement selector honors snoozed sources.
- Startup health now exists: boot probes DB integrity, scheduler jobs, MCP warm pool, Google OAuth, graph outbox, media outbox, backups, and recent log errors, then can DM the owner on degradation.
- A separate dead-man script can alert through a separate Telegram bot if the main agent, DB freshness, backups, external MCP, or tunnel degrade.
- Daily check-in can be scheduled/queried/skipped through parsed text and pending replies are pre-routed before normal chat.
- Relevant offline tests pass for the cockpit, memory, approval, health, daily check-in, reminders, and proactive paths.

The remaining gap is the Telegram-native control surface. The current cockpit is still command/text based. There is no command menu registration, no callback query handler, no inline keyboards/buttons, no `/reminders` command, no `/checkin` command, and several operator messages are either misleading or weaker than the underlying system. The owner can inspect and tune many things when the bot is alive, but recovery still often ends in launchd/log commands, and a few Telegram commands overstate what they do.

Note on priors: I read the requested prior reports at the start of the review. During the review, `codex/` changed externally and those files disappeared from the working tree; I did not restore or rely on them as truth.

## 2. Findings, ordered P0/P1/P2/P3

### P0

No P0 Telegram UX/operator-cockpit issue found in the current source.

### P1

**P1-1: `/approvals` tells the user to type confirmation forms that the resolver rejects.**

- Evidence: `cmd_approvals` prints `CONFIRM-SEND <id> | REJECT <id> | /approvals cancel <id>` at `agents/telegram_bridge.py:1755-1762`.
- Actual resolver: approval succeeds only when inbound text equals the exact configured phrase, currently bare `CONFIRM-SEND`; reject succeeds only on `cancel`, `stop`, or `abort` from `config/engagement.yaml:64-73`; `tools/approvals.py:155-184`.
- Gatekeeper prompt agrees with the resolver and says `type CONFIRM-SEND exactly`, not with `/approvals`; see `tools/gatekeeper.py:264-269`.
- Impact: if the owner follows `/approvals` and types `CONFIRM-SEND 123`, the approval is rejected and the message is then allowed to continue as normal chat (`tools/approvals.py:173-184`). That is a high-trust failure right at the risky-operation gate.
- Fix direction: make `/approvals` copy match the resolver, or update the resolver to accept the documented id-scoped forms and consume all approval-looking replies.

**P1-2: There is still no Telegram callback/button layer for deterministic operator flows.**

- Evidence: `agents/telegram_bridge.py` imports and registers `CommandHandler`, `MessageHandler`, and `MessageReactionHandler`, but no `CallbackQueryHandler`; see imports at `agents/telegram_bridge.py:19-28` and handler registration at `agents/telegram_bridge.py:2244-2285`.
- Repo search found no `CallbackQueryHandler`, `InlineKeyboardMarkup`, or `set_my_commands` implementation.
- Telegram officially supports inline keyboards and `answerCallbackQuery`; button callbacks are the right primitive for low-risk choices, while typed confirmation can remain for high-risk sends/deletes.
- Impact: approvals, daily check-in choices, proactive snoozes/feedback, reminders, settings toggles, and audit/status navigation all require typed commands or natural-language parsing. That is operable, but not yet a Telegram cockpit.
- Fix direction: add a callback token ledger and owner-gated callback handler. Start with safe actions: approval cancel/details, daily check-in email/calendar/both/skip, reminder snooze/cancel/list, proactive why/snooze/quiet, and status refresh.

**P1-3: Reminders and daily check-ins do not have deterministic command surfaces.**

- Evidence: reminder tools exist (`tools/reminders/list.py`, `cancel.py`, `snooze.py`, `create.py`) and the scheduler fires/syncs reminders at `agents/scheduler.py:40-78`, but `build_application()` registers no `/reminders` command; see `agents/telegram_bridge.py:2249-2266`.
- Daily check-in has scheduler and text pre-router support at `agents/daily_checkin.py:636-667` and `agents/daily_checkin.py:709-746`, but there is no `/checkin` command in the handler list.
- README command docs also list no `/reminders` or `/checkin`; see `README.md:330-349`.
- Impact: the owner can ask Hikari in natural language to list/cancel/snooze reminders, but cannot deterministically inspect and operate the reminders queue from Telegram if the model/tool-routing path is degraded. Daily check-in can be edited with magic phrases, but not discovered or managed through a command.
- Fix direction: add `/reminders` with list/snooze/cancel and `/checkin` with status/time/skip/resume/set. Back those with the existing DB/tool helpers rather than an LLM turn.

### P2

**P2-1: `/status` is useful but its Google OAuth line is not the same health signal used by scheduler startup.**

- Evidence: `/status` calls `cockpit.format_status()`, which reports OAuth via `_oauth_states()`; that only checks whether a grant blob exists in keychain (`agents/cockpit.py:192-200`).
- Startup/scheduler health uses `probe_google_token()` and writes/reads `runtime_state.calendar_heartbeat_healthy` (`agents/telegram_bridge.py:2391-2409`, `agents/scheduler.py:386-408`, `agents/google_health.py:34-75`).
- README says `/status` returns the live OAuth probe state (`README.md:263-266`), but the code currently reports keychain presence, not the probe result.
- Impact: `/status` can say Google has a cached grant while the refresh token is invalid, or say `no grant` while env-var based Google Workspace credentials are healthy. That weakens the owner's ability to debug OAuth from Telegram.
- Fix direction: show the exact runtime probe value (`calendar_heartbeat_healthy`) and/or rerun the cheap token probe on demand with clear states: ok, missing env, invalid_grant, network, stale/unknown.

**P2-2: `/settings set GRAPHITI_ENABLED false` implies a live toggle, but the scheduler only decides the graph drain job at boot.**

- Evidence: the setting writer stores `settings.GRAPHITI_ENABLED` and updates `os.environ["GRAPHITI_ENABLED"]` (`agents/cockpit.py:63-75`).
- The graph outbox drain job is registered once in `build_scheduler()` only if `GRAPHITI_ENABLED` is not false at that moment (`agents/scheduler.py:362-381`).
- There is no code path that removes or adds the already-registered scheduler job when `/settings` changes this value.
- Impact: Telegram can return `ok. GRAPHITI_ENABLED = false` while the existing `graph_outbox_drain` job continues running until restart. That is a trust/cockpit accuracy bug.
- Fix direction: either make the setting explicitly "takes effect after restart" or have the setter pause/remove/add the scheduler job.

**P2-3: `/cancel` marks a background task cancelled but does not actually interrupt the running session.**

- Evidence: `cmd_cancel` updates the DB row to `cancelled` and tells the user it will finish its current turn before stopping (`agents/telegram_bridge.py:1412-1440`).
- The dispatch runner does not check for a cancelled row inside its receive loop; it proceeds to mark `done` or `failed` when the SDK session completes (`tools/dispatch/_shared.py:109-180`).
- Impact: from Telegram, the owner can mark a task cancelled but cannot stop runaway cost/time immediately. This is honest in the command reply, but the README command table still says `/cancel` cancels a pending in-flight tool call (`README.md:338-339`).
- Fix direction: add cooperative cancellation checks in `_run_session()` and/or retain task handles for real cancellation. Update docs until that exists.

**P2-4: There is no registered Telegram command menu, so discoverability still depends on remembering `/help`.**

- Evidence: `build_application()` registers handlers but no bot command menu setup (`agents/telegram_bridge.py:2244-2285`), and `post_init` also does not call `set_my_commands()` (`agents/telegram_bridge.py:2338-2497`).
- Telegram Bot API provides `setMyCommands` for exactly this menu surface.
- Impact: the text cockpit exists, but Telegram clients will not show the command list unless it has been configured elsewhere by BotFather/manual state. A local rebuild/new bot can silently lose discoverability.
- Fix direction: register owner/private-scope commands at boot, still keeping runtime owner checks.

### P3

**P3-1: `/help` still advertises debug/operator-only commands in the main command list.**

- Evidence: `_COMMANDS` includes `/memory_diff` and `/grab_stickers` alongside user-facing controls (`agents/cockpit.py:23-40`).
- Impact: not dangerous for a single-owner bot, but it makes the cockpit feel like a debug console rather than a clean operator surface.
- Fix direction: split `/help` into common commands and "debug/operator" commands, or hide debug-only commands from menu registration.

**P3-2: Recovery UX is better, but still mostly "alert, then SSH."**

- Evidence: startup health can DM degraded checks (`agents/health.py:219-281`, `agents/telegram_bridge.py:2415-2433`), and dead-man alerts can fire from a separate bot (`scripts/dead_man.py:1-6`, `scripts/dead_man.py:83-99`).
- Recovery recipes in README still require launchd, logs, curl, sqlite, and restart commands (`README.md:270-326`).
- Impact: if Hikari is alive, `/status` is enough for first-pass inspection. If Hikari is down, Telegram can alert but cannot restart, tail logs, restore, or rerun probes remotely.
- Fix direction: decide whether "without SSH" includes remote repair. If yes, add a very small, separately-gated dead-man control bot or documented Shortcuts/LaunchAgent actions for restart/status only.

## 3. Previously reported issues that now look closed

- **Missing text cockpit commands:** closed for `/help`, `/status`, `/tools`, `/audit`, and `/settings`. Implemented in `agents/cockpit.py` and registered in `agents/telegram_bridge.py:2249-2263`.
- **No user-friendly memory surface:** mostly closed. `/memory` now supports recent/search/fact/forget/correct/session/why/debug (`agents/telegram_bridge.py:1494-1699`) with dedicated tests in `tests/test_telegram_memory_cmd.py`.
- **No proactive source controls:** mostly closed for text commands. `/proactive status/on/off/recent/why/snooze` exists (`agents/telegram_bridge.py:1790-1882`), and snooze is enforced by selector state (`agents/engagement/selector.py:22-49`, `agents/engagement/selector.py:137-147`).
- **No startup health digest:** closed. Health checks and digest gating are implemented in `agents/health.py:219-281` and wired in `agents/telegram_bridge.py:2415-2433`.
- **Daily check-in only as a concept:** closed for the core routine. Scheduler, firing, pending reply handling, schedule edits, and tests exist (`agents/daily_checkin.py`, `tests/test_daily_checkin_bridge.py`, `tests/test_daily_checkin_intent.py`, `tests/test_daily_checkin_schedule.py`).
- **Approval queue absent:** partially closed. `/approvals` can list and cancel pending gatekeeper rows (`agents/telegram_bridge.py:1702-1762`) and tests cover empty/list/cancel paths (`tests/test_phase_f_gatekeeper_features.py:104-250`).
- **Proactive observability absent:** partially closed. `/proactive recent` and `/proactive why` expose event rows and payload previews (`agents/cockpit.py:535-578`).

## 4. New regressions or contradictions

- `/approvals` instruction text contradicts the gatekeeper resolver and prompt. This is the highest-priority contradiction because it affects high-risk approvals.
- README says `/status` shows OAuth probe state, but `/status` only reports keychain grant presence.
- `/settings` stores `settings.GRAPHITI_ENABLED` as if it were a live toggle, while graph drain job registration is boot-time only.
- README says `/cancel` cancels an in-flight tool call, while the implementation only updates the DB row and does not interrupt `_run_session()`.
- The old prior files requested by this review were available at the beginning of the run, then disappeared from the working tree while I was reviewing. Current `git status --short` shows those deletions and `codex/index.md` modified. I treated that as external/user work and did not revert it.

## 5. Missing tests / suggested verification

Tests run:

- `uv run python -m pytest tests/test_telegram_cockpit_cmds.py tests/test_telegram_memory_cmd.py tests/test_phase_f_gatekeeper_features.py tests/test_gatekeeper.py tests/test_daily_checkin_bridge.py tests/test_daily_checkin_intent.py tests/test_daily_checkin_schedule.py tests/test_health.py tests/test_bridge_ux.py -q`
  - Result: 143 passed, 1 warning.
- `uv run python -m pytest tests/test_smoke.py tests/test_proactive_global_reservation.py tests/test_reminders_scheduler.py tests/test_reminders_tool.py tests/test_daily_checkin_fetch.py tests/test_proactive_feedback.py tests/test_audit_redaction_widened.py tests/test_approval_preview_truthful.py -q`
  - Result: 98 passed, 1 warning.

Missing/high-value tests:

- Add approval resolver tests for documented `/approvals` forms:
  - `CONFIRM-SEND <id>` should either approve or the UI must stop documenting it.
  - `REJECT <id>` should either reject or the UI must stop documenting it.
  - Any approval-looking reply that fails validation should be consumed and should not route into normal chat.
- Add a structural test that `build_application()` registers the full intended owner command list and a `CallbackQueryHandler` once buttons exist.
- Add a startup/post-init test that `set_my_commands()` is called with the menu commands.
- Add `/reminders` command tests for list/snooze/cancel and active/all views.
- Add `/checkin` command tests for status, set default time, skip today/tomorrow, and pending-state display.
- Add a `/settings GRAPHITI_ENABLED` behavioral test that proves the scheduler job is stopped/started or that the reply says restart required.
- Add a `/status` OAuth test where `calendar_heartbeat_healthy=0:invalid_grant` appears in the output.
- Add a dispatch cancellation test proving a cancelled row interrupts `_run_session()` before final `done`, or adjust docs/tests to assert "mark only."
- After inline buttons land, run an integration-style fake Telegram callback test that every callback verifies owner id and answers callback queries.

Suggested manual verification on a live bot:

- `/help`, `/status`, `/tools`, `/tools recent`, `/audit recent 5`, `/settings`, `/memory recent`, `/proactive status`, `/proactive recent`, `/approvals`.
- Create a fake gated action and verify the prompt, `/approvals`, typed `CONFIRM-SEND`, typed `cancel`, and timeout behavior match exactly.
- Create two reminders, then verify Telegram can list, snooze, and cancel them without relying on the LLM path once `/reminders` exists.
- Trigger startup health with a deliberately invalid Google refresh token and confirm both startup digest and `/status` surface the same reason.

## 6. Sprint or roadmap implications

- Treat the text cockpit as Sprint 6A/6D mostly done, with a quick bugfix pass needed before productizing it.
- Next highest-leverage sprint: **Telegram buttons + command menu**. Implement the callback ledger, owner-gated `CallbackQueryHandler`, `answerCallbackQuery`, and `set_my_commands`.
- Before buttons, fix the `/approvals` copy/resolver mismatch. This is small and prevents the most confusing trust failure.
- Add deterministic `/reminders` and `/checkin` commands before adding new companion capabilities. These are more useful for owner trust than another proactive source.
- Reconcile `/status` and startup health so the cockpit is a real source of truth for OAuth/integration state.
- Decide the operational boundary: if "without SSH" means "inspect and pause while alive," the current direction is close. If it means "recover when dead," the roadmap needs a separate dead-man control channel or a documented macOS Shortcut/LaunchAgent restart bridge.

## 7. Sources used

Local source, tests, config, and docs:

- `agents/telegram_bridge.py`
- `agents/cockpit.py`
- `agents/daily_checkin.py`
- `agents/scheduler.py`
- `agents/health.py`
- `agents/google_health.py`
- `agents/engagement/selector.py`
- `agents/background_listener.py`
- `tools/approvals.py`
- `tools/gatekeeper.py`
- `tools/dispatch/_shared.py`
- `tools/reminders/*.py`
- `config/engagement.yaml`
- `README.md`
- `scripts/dead_man.py`
- `tests/test_telegram_cockpit_cmds.py`
- `tests/test_telegram_memory_cmd.py`
- `tests/test_phase_f_gatekeeper_features.py`
- `tests/test_gatekeeper.py`
- `tests/test_daily_checkin_bridge.py`
- `tests/test_daily_checkin_intent.py`
- `tests/test_daily_checkin_schedule.py`
- `tests/test_health.py`
- `tests/test_bridge_ux.py`
- `tests/test_reminders_scheduler.py`
- `tests/test_reminders_tool.py`
- `tests/test_proactive_feedback.py`
- `tests/test_approval_preview_truthful.py`

Prior context read at review start:

- `codex/telegram-ux-design-2026-05-23.md`
- `codex/ux-review-what-user-wants-2026-05-23.md`
- `codex/ops-production-runbook-2026-05-23.md`

Official external sources:

- Telegram Bot API, `answerCallbackQuery` and `setMyCommands`: https://core.telegram.org/bots/api#answercallbackquery and https://core.telegram.org/bots/api#setmycommands
- Telegram Bot Features, commands and inline keyboards: https://core.telegram.org/bots/features#commands and https://core.telegram.org/bots/features#inline-keyboards
