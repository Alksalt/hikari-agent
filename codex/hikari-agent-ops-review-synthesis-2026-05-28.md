# Hikari Agent Ops Review Synthesis - 2026-05-28

## Scope

Second review pass over ops and deep-system surfaces that were not covered by the first 15-lane audit. No source files were edited. Earlier reports in `codex/` were left intact.

Reviewed lanes:

1. MCP/external runtime
2. Google Workspace deep audit
3. Apple/macOS integrations
4. Backup/restore/disaster recovery
5. SQLite migrations and graph consistency
6. Scheduler/APScheduler reliability
7. Config/policy drift
8. Photo/media privacy
9. Attachment/file ingest
10. Cost/budget/quota
11. Concurrency/locks
12. Prompt/context construction
13. Deployment/ops runbook
14. User controls/docs/reversibility
15. End-to-end red-team synthesis

## Bottom Line

No P0 was found. The strongest P1 cluster is not a single catastrophic bypass; it is a set of "control-plane lies": settings, docs, status surfaces, and approval/proactive controls that look stronger or broader than the runtime actually enforces.

Most important themes:

- Approval semantics are split. `confirm_send` can be configured as gated but not resolved like `gatekeeper`, `/status` can count approvals that `/approvals` does not list, and timeout config is split between visible and live paths.
- "Global" proactive controls are not global. They mostly affect the unified engagement producer tick, while ceremony/background sends such as morning brief and decision resolver use separate gates.
- Privacy defaults around media are too eager. Uploaded photos can be routed to third-party vision/geocoding before a clear per-upload privacy decision, and raw media lacks a retention control.
- Durable outbox paths lack claim/lease semantics. `media_outbox` and `graph_outbox` can duplicate, replay, or lose operator trust after partial failures.
- Cost reporting is fragmented. Persistent SDK calls bypass per-call `max_budget_usd`; aux/model/media costs are not in one ledger; status mixes old and new accounting.
- Several recovery docs are stale enough to slow incident response, including a missing `scripts.drain_outbox` command and an `.env` recovery snippet with the wrong Telegram token variable.

## Highest Priority Fixes

1. Make approval policy one state machine.
   Validate `gate` and `access_mode` enums at registry load. Either remove `confirm_send` as a runtime gate or route it through `GATEKEEPER.resolve()` exactly like `gatekeeper`. Make `/status`, `/approvals`, callback rows, and typed `CONFIRM-SEND <id>` agree on the same pending set.

2. Define what "proactive off" means and enforce it centrally.
   If it means "no unsolicited messages," add a global check to `reserve_and_send()`, with explicit exceptions for user-created reminders. If it means "only engagement producers," rename the setting and UI text.

3. Add media privacy gates before third-party calls.
   Wire `photo_in.enabled` and EXIF/geocoding switches into runtime. Default third-party classification/geocoding to off or ask-first for personal photos, screenshots, receipts, and GPS-bearing images. Add raw media TTL cleanup.

4. Add atomic claims for outbox drains.
   `media_outbox` should claim `pending -> processing` before Telegram sends and should include chat scoping. Direct MCP calls need timeout/reset. `graph_outbox` needs claim/lease or idempotent replay semantics.

5. Unify cost telemetry and make unknown pricing visible.
   Use `llm_costs` plus provider/media/aux rows as the single status source. Treat unknown models as "unpriced," not `$0.00`. Decide whether persistent SDK turns can be budget-enforced; if not, say so in status.

6. Fix incident-response docs before the next incident.
   Replace the missing graph drain command, correct `HIKARI_BOT_TOKEN` to `TELEGRAM_BOT_TOKEN`, and align README health thresholds with code.

## Verification Run In This Pass

- `python3 scripts/regen_subagent_policy.py --check` - passed for five prompt files.
- `python3 scripts/validate_tool_registry.py` - exited clean under system Python but printed import-skip warnings, so this result is weak unless CI runs it in the provisioned `uv` env.
- `uv run python -m pytest tests/test_inject_memory.py tests/test_inject_memory_cull.py tests/test_inject_memory_entrypoint_aware.py tests/test_now_block_contract.py tests/test_tool_inventory.py tests/test_layer_b_injection_corpus.py -q` - `38 passed, 1 warning`.
- `uv run python -m pytest tests/test_busy_timeout.py tests/test_sqlite_connection_pool.py tests/test_async_subprocess_wrappers.py tests/test_recycle_under_run_lock.py tests/test_mcp_manager_call.py tests/test_reminders_scheduler.py -q` - `36 passed`.
- `uv run python -m pytest tests/test_telegram_cockpit_cmds.py tests/test_set_my_commands.py tests/test_callbacks_owner_gated.py tests/test_approvals_resolver_accepts_id.py tests/test_phase_i_proactive.py -q` - `111 passed, 1 warning`.
- One MCP lane reported a broader targeted run: `90 passed, 2 deselected, 1 warning`.

## Report Files

- `codex/hikari-agent-ops-review-synthesis-2026-05-28.md`
- `codex/hikari-agent-ops-review-lane-findings-2026-05-28.md`
- `codex/hikari-agent-ops-review-backlog-2026-05-28.md`
