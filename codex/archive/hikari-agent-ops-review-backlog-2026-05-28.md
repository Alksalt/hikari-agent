# Hikari Agent Ops Review Backlog - 2026-05-28

## P1 - Fix First

- Approval unification: make `confirm_send` either policy-only or a first-class `GATEKEEPER.resolve()` path. Add typed `CONFIRM-SEND <id>` tests for both gate kinds.
- Registry validation: fail load on unknown `gate` or `access_mode`; add fixture tests for typos.
- Approval UX truth: make `/status`, `/approvals`, approval callbacks, typed approvals, and restart recovery use the same pending-row query.
- Global proactive semantics: decide whether `proactive.enabled=false` means "no unsolicited messages." If yes, enforce it in `reserve_and_send()` with explicit reminder exceptions.
- Media chat scoping: store required `chat_id` in `media_outbox`, fetch pending rows by chat, and abort missing/mismatched rows.
- Media duplicate-send window: add a terminal `delivery_uncertain` state for Telegram-success/DB-failure cases.
- Media privacy gates: wire `photo_in.enabled`, EXIF geocode, and raw media retention controls before Anthropic/Nominatim calls.
- Attachment wrapping: wrap model-visible PDF/image/native blocks with the same untrusted boundary contract as text/HTML.
- MCP direct calls: add configurable per-call timeout, reset/close cached session on timeout or transport error, and evict direct-call sessions on TTL.
- Persistent SDK retry cleanup: clear poisoned live clients after second failure.
- Compound turn ContextVars: return child tool-call sets and aggregate them before final post-filtering.
- Cost enforcement decision: either route budgeted calls through a budget-enforced client or rename the UI to quota telemetry.
- Research worker budgeting: add model, fallback, max budget, and `llm_costs` persistence.
- Aux/media cost ledger: track OpenRouter aux calls, image generation, vision classifier, voice/STT, and unknown/unpriced rows in the same status surface.
- Backup source path: make scripts honor `HIKARI_DB_PATH`.
- Restore safety: stop services, validate archive contents, validate restored DB, and clean decrypted temp secrets.
- DB migrations: add cross-process migration lock and enable SQLite foreign keys on pooled connections.
- `graph_outbox`: add claim/lease semantics and include Kuzu backup/restore or forced replay.
- Scheduler catch-up: persist enough job/fire state to avoid recurrence duplication after downtime.
- Runbook correctness: fix wrong `HIKARI_BOT_TOKEN`, replace missing `scripts.drain_outbox`, and align README thresholds with `agents/health.py`.

## P2 - Harden Next

- Add `/settings reset <key>` or `unset <key>` for runtime overrides.
- Validate `/settings set proactive.enabled <json_list>` against `ALL_PRODUCER_IDS`.
- Validate `/proactive snooze <source>` against known producers; add "snooze all" only if implemented centrally.
- Make `/proactive status` show active snoozes and next quiet window, or delete dead `format_proactive_status()`.
- Use `cockpit.format_silence_ack()` in `/silence` so the user sees an expiry timestamp.
- Rewire `/reminders` to use `format_reminders_page()` and preserve pagination buttons.
- Attach `/receipt` filter buttons or remove the promise from docs.
- Fix `/memorydump` pagination callback indexing and keep the keyboard on page changes.
- Add undo/review affordances for destructive memory buttons (`Forget`, `Pin`) or require a second tap.
- Add slash-accessible privacy toggles for photo routing, EXIF geocoding, raw media TTL, voice retention, and generated-photo proactives.
- Add link shelf `/links delete <id>` and `/links update <id>` controls, or document natural-language deletion clearly.
- Make `/tools` default match docs: either summary or policy, not both depending on call path.
- Treat unknown model pricing as unpriced, not free; surface unpriced rows in `/status`.
- Persist cache TTL detail or raw usage JSON for later cost audits.
- Add source clamps to external MCP read tools.
- Add host allowlist for provider-returned image URLs.
- Add retention cleanup for `data/user_photos`, `data/user_documents`, and voice files.
- Add symlink and TOCTOU tests for attachment readers.
- Remove engagement-level prompt-injection override branches or make them test-only.
- Include wildcard-gated tools in generated subagent policy when a subagent allowlist can reach them.
- Move state mutation in prompt context formatters until after selection and successful send.
- Sanitize `self_model` and unknown core block labels the same way as peer updates.
- Change tool inventory language from `configured` to `env present, auth unverified` unless health checked.
- Add subprocess runner utility with timeout, terminate, kill, bounded drain, and return code capture.
- Reset turn ContextVar tokens in `finally` and avoid mutable set defaults.
- Start SDK pool before scheduler or make SDK startup idempotent when a job connects early.

## Regression Guardrails

- Approval: typed approve/reject by row id, hidden/non-gatekeeper rows, restart-recovery survivor rows, and `/status` count parity.
- Proactive controls: `proactive.enabled=false` suppresses every intended unsolicited path; tests should explicitly cover morning brief, decision resolver, engagement tick, and user-created reminders.
- Media: `photo_in.enabled=false` blocks classifier and EXIF; fake GPS image does not call Nominatim; raw media TTL deletes saved files; outbox chat mismatch does not send.
- Attachments: native PDF/image block prompt-injection corpus, unknown image MIME fail-closed, no raw filename prompt breakouts, size limit after download/base64.
- Cost: persistent turn budget behavior is either enforced or explicitly reported unavailable; OpenRouter usage rows are inserted; unknown pricing alerts.
- Outboxes: two concurrent media drains send once; `media_outbox_mark_sent` failure after Telegram success does not retry private media; graph outbox claims are single-consumer.
- Prompt context: `respond()` integration emits `# gap_since_last`; handoff/header-forgery strings are escaped; culling does not consume dropped blocks.
- User controls: `/reminders` pagination buttons work; `/receipt` category buttons work; `/memorydump Next` advances; invalid proactive source lists are rejected.
- Ops docs: smoke-test README commands in dry-run where possible; assert documented scripts exist; assert documented env vars match runtime-required env vars.
