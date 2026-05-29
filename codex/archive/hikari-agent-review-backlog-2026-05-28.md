# Hikari Agent Review Backlog - 2026-05-28

This is the implementation/test backlog distilled from the 15-agent review.

## Phase 0 - Stop The Highest-Risk Failure Modes

1. Gate or remove `skill_approve` and `run_skill`.
   - Require typed owner approval with staged id and content hash.
   - Prevent same-turn create/approve/run.
   - CI assert skill tools are never `gate: null`.
   - Tests: untrusted content tries `skill_create`/`skill_approve`; must be denied.

2. Two-phase live SDK session commit.
   - Generation returns `reply_text`, `session_id_before`, `candidate_session_id`.
   - Commit `candidate_session_id` only after Telegram send and DB persistence.
   - On guard/drop/send failure, restore prior session id and reconnect pool.
   - Tests: send failure, quiet-hour proactive drop, guard rejection.

3. Handler-level safe logging.
   - Attach redaction/canary/turn-id filters to every handler or use a safe `LogRecordFactory`.
   - Never emit raw canary; replace with fixed leak summary and hash.
   - Tests: child logger emits token/canary; handler output is redacted.

4. Fix Layer C rubric scale.
   - Use `weighted_avg >= 3.0` or normalize everything to `0-1`.
   - Add regression for intentionally weak 1/4 rubric output.

5. Persona override stack.
   - Safety, truth, tool fidelity, user agency, crisis > character, romance, silence, terseness, flirt.
   - Add crisis eval: rude/panicked/self-harm-adjacent input cannot route to `ask nicely` or silence.

## Phase 1 - Tool Governance And Approval

1. Fail closed for utility tools.
   - Require every discovered `mcp__hikari_utility__*` tool to have an explicit `config/tools.yaml` entry.
   - Remove or deny-by-default the wildcard read/gate-null fallback.
   - Test with fake discovered write tool.

2. Split local reminders from synced/action reminders.
   - Gate GCal/Apple sync and `kind="action"`.
   - Persist approval id/content hash on reminder rows.
   - Scheduler refuses unapproved sync/action rows.
   - `MANAGER.call` enforces write policy unless given narrow approved capability token.

3. Repair Apple Events approval.
   - Convert `confirm_send` to `gatekeeper`, or fully implement confirm-send list/resolve/cancel.
   - Ensure `/approvals` shows Apple write rows.

4. Route progress/voice/photo/Notes through policy.
   - Progress text: templated or post-filtered.
   - Gate `voice_outbound_send(force=true)`.
   - Scan media prompts for PII/secrets before external vendors.
   - Gate or narrow Apple Notes writes.

5. Fail closed for untrusted wrapping exceptions.
   - If wrapping crashes for matching external/untrusted tools, provide a safe error block to the model, not raw content.

## Phase 2 - Runtime, Delivery, And Persistence

1. Split send result into `sent_ok` and `persisted_ok`.
   - If sent but DB persist failed, write repair/dead-letter row.
   - Skip handoff/postsend/reflection/observation surfacing when persistence fails.

2. Return `SendResult` from choreography helpers.
   - Photo/voice episodes only after `sent_ok and persisted_ok`.
   - Persist delivered final text, not pre-filter draft.

3. Fix `_unpack_send_result`.
   - `False` returns `ok=False`.
   - Unknown malformed return fails closed or logs loudly and returns `ok=False`.

4. Add state-machine tests.
   - `tests/test_runtime_persistent_recovery.py`
   - `tests/test_runtime_session_lock_trajectory.py`
   - `tests/test_internal_control_isolation_trajectory.py`
   - `tests/test_voice_bridge_trajectory.py`
   - `tests/test_photo_bridge_security_trajectory.py`

## Phase 3 - Memory And Reflection

1. Add `FactEvidence` validator.
   - Source message exists in current reflection window.
   - `source_text` matches cited message span.
   - Attribution rank is enforced: inferred/subagent facts cannot supersede user-stated/user-corrected facts without direct user evidence.

2. Sanitize and wrap self-model.
   - Sanitize all string/list fields before upsert.
   - Render in remembered/untrusted wrappers.
   - Skip block on sanitizer failure.

3. Fix invalidation/supersession integrity.
   - Validate `fact_id` exists and is active.
   - Validate `superseded_by` exists and is active.
   - Return error on zero-row updates.
   - Enable SQLite foreign keys or enforce in helper code.

4. Centralize bitemporal active checks.
   - Replace string comparisons and ad hoc `valid_to IS NULL`.
   - Fix graph recall supplement duplicate check to use `Hit.ref_id`.

5. Test targets.
   - Reflection rejects absent source ids.
   - Reflection rejects nonmatching source text.
   - Reflection refuses inferred-over-user-stated supersession.
   - Self-model injection canary is not persisted or injected.
   - Graph+SQLite supplement does not duplicate facts.

## Phase 4 - Persona And Eval System

1. Make `PERSONA.md` the source of truth.
   - Skill files are examples only.
   - Relationship stage, comfort, anger, refusal, safety/tool gates, consent/stop, and warmth budgets cannot be overridden by skill files.

2. Split `INTIMATE.md`.
   - Flirt examples.
   - Emotional disclosure examples.
   - Intimate examples.
   - Each starts with allowed stages, disallowed modes, required user signal, cooldown.

3. Restructure lore.
   - Ambient always-on.
   - Contextual on-demand.
   - Dormant/private.
   - Static tests prevent private/relative-time facts in always-on lore.

4. Expand anti-sycophancy.
   - Add deterministic fast lane plus nightly live lane.
   - Categories: false factual premise, hard-opinion anchor pressure, compliment/flattery, emotional dependence, moral face-saving, user-as-wronged framing.
   - Metrics: HOLD/YIELD, Turn of Flip, Number of Flips, final stance, recovery.

5. Eval harness fixes.
   - Full transcript scoring for multi-turn cases.
   - Discover real trajectory cases.
   - Generated-output evals with seeded DB and mocked tools.
   - Judge calibration set with 80-120 labeled samples.
   - Survival metrics for 50/100/200-turn sessions.

## Phase 5 - Proactive, Research, And Product Value

1. One proactive source registry.
   - Active/inactive/snoozed/deferred, pool, send mode, last sent, next eligible, and reason contract all come from one source.
   - `/proactive status` explains sources that are enabled but not collected.

2. Standardize producer consumption.
   - Every producer implements `mark_consumed(candidate)`.
   - IDs come from `candidate.payload`.
   - Add scheduler-level duplicate-send soak tests.

3. Fix cadence and reason contract.
   - `sender.send` passes `candidate` to `reserve_and_send`.
   - Record by `candidate.pool`, not always user-anchored.
   - Every proactive row has `anchor`, `why_now`, `confidence`, `controls_json`, and `data_checked_json` where expected.

4. Source-aware quiet hours.
   - Low/medium sources blocked.
   - High-priority user-opted-in sources may pass with strict dedup and reason logging.

5. Structured research callbacks.
   - Store `summary`, `claims[]`, `sources[]`, `fetched_at`, `confidence`, `failure_reason`.
   - Retry transient failures separately from true no-source results.
   - Wrap `summary_excerpt` and `sources_json` as untrusted.
   - Add dedicated `research_callback` template requiring URL or "could not verify".

6. `send_mode: observation`.
   - Observation never interrupts.
   - Queue into next-turn context or digest.

## Phase 6 - Telegram UX And Observability

1. Fix keyboard/callback contracts.
   - Attach receipt/diary keyboards.
   - Add missing `receipt:` and `diary:` callbacks.
   - Normalize page indexing for memory/reminders.

2. Emotional-safety gate for nonverbal surfaces.
   - Reactions, stickers, callbacks, and progress text suppress playful/noisy behavior on vulnerable/heavy turns.
   - User stickers outside capture mode get a lightweight acknowledgment, not silence.

3. Split companion surface from operator cockpit.
   - `/status`, `/tools`, `/audit`, `/settings` can stay technical.
   - Daily-user commands should avoid backend terms and raw IDs unless needed.

4. Health and telemetry.
   - Structured health states: `ok`, `degraded`, `disabled`, `unknown`, `transient`.
   - Distinguish invalid credentials from transient network failures.
   - Add timezone-aware logs and count `ERROR|CRITICAL`.
   - Add `turn_id`, `session_id`, `tool_use_id`, entrypoint, redacted input shape/hash, semantic status to tool telemetry.

## Top 20 Tests/Evals To Add

1. `tests/test_runtime_persistent_recovery.py`
2. `tests/test_runtime_session_lock_trajectory.py`
3. `tests/test_internal_control_isolation_trajectory.py`
4. `tests/test_gatekeeper_taint_trajectory.py`
5. `tests/test_gatekeeper_canary_hard_deny.py`
6. `tests/test_apple_events_confirm_send_gate.py`
7. `tests/test_gatekeeper_prompt_failure.py`
8. `tests/test_tool_registry_untrusted_wrap_coverage.py`
9. `tests/test_layer_b_runtime_gate.py`
10. `tests/test_compound_turn_write_ordering.py`
11. `tests/test_compound_turn_child_tool_aggregation.py`
12. `tests/test_voice_bridge_trajectory.py`
13. `tests/test_photo_bridge_security_trajectory.py`
14. `tests/test_bridge_router_precedence.py`
15. `tests/test_engagement_tick_integration.py`
16. `tests/test_engagement_cofire_replay.py`
17. `tests/test_proactive_gate_failure_states.py`
18. `tests/persona/test_personagym_corpus.py`
19. `tests/persona/test_drift_correction_loop.py`
20. `tests/test_layer_b_low_risk_write_bypass.py`

## Suggested First Patch Set

1. `config/tools.yaml`, `tools/skills/core.py`, `tools/gatekeeper_can_use_tool.py`: gate persistent skill operations and fail closed on unregistered utility tools.
2. `agents/runtime.py`, `agents/messaging.py`, `agents/telegram_bridge.py`, `agents/proactive_gate.py`: delivery transaction/session commit refactor.
3. `agents/log_scrub.py`, `agents/telegram_bridge.py`, `tests/test_security.py`: handler-level redaction and canary redaction tests.
4. `evals/conversation/runner_layer_c.py`, `evals/conversation/cases/layer_c/*.yaml`, `tests/test_layer_c_runner.py`: rubric scale fix.
5. `assets/PERSONA.md`, `.claude/skills/character-voice/*`: override stack and skill-gate authority.

