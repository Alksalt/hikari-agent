# Hikari Agent Ops Review Lane Findings - 2026-05-28

No P0 findings were reported in this pass.

## Lane 1 - MCP / External Runtime

P1 - OAuth can mint tokens that runtime later rejects.
Evidence: `mcp_external/oauth.py:400`, `mcp_external/oauth.py:633`, `mcp_external/launch.py:175`. The OAuth path allows missing/optional resource/audience context, while middleware expects an audience match.

P1 - Public host handling is split.
Evidence: `config/engagement.yaml:591`, `mcp_external/server.py:143`. `PUBLIC_BASE_URL` exists in config, but server allow-host behavior reads a different source.

P1 - Direct MCP calls can hang or poison cached sessions.
Evidence: `agents/mcp_manager.py:219`, `agents/mcp_manager.py:226`, `agents/mcp_manager.py:230`, `agents/mcp_manager.py:186`. Cached sessions have no per-call timeout, are not reset on exceptions, and TTL eviction does not close cached direct sessions.

P1 - MCP drift validator has false negatives.
Evidence: `tools/mcp_introspect.py:42`, `tools/mcp_introspect.py:65`, `scripts/validate_mcp_servers.py:73`. Placeholder envs are not expanded, JSON-RPC errors can become empty toolsets, and soft skips can hide declared-not-live drift.

P2 - OAuth-only launch can fail authorization because cookie signing source is missing.
Evidence: `mcp_external/launch.py:280`, `mcp_external/oauth.py:111`.

P2 - Rate limiting is narrow.
Evidence: `mcp_external/oauth.py:475`, `mcp_external/oauth.py:281`, `mcp_external/oauth.py:134`. `/register` is open and Cloudflare forwarded IP is ignored.

P2 - External read tools do not clamp limits.
Evidence: `mcp_external/server.py:162`, `mcp_external/server.py:180`, `mcp_external/server.py:204`, `mcp_external/server.py:251`.

P2 - Playwright uses a fixed temp profile.
Evidence: `config/tools.yaml:129`.

P2 - OAuth 2.1 tokens are stored plaintext.
Evidence: `storage/db.py:4529`, `storage/db.py:4579`.

## Lane 2 - Google Workspace

P1 - Reminder-created Google Calendar events bypass Workspace write gates.
Evidence: `tools/reminders/create.py:89`, `tools/reminders/create.py:150`, `agents/proactive.py:512`, `tools/reminders/sync_gcal.py:91`, `agents/mcp_manager.py:204`, `config/tools.yaml:1104`. Reminder sync calls Calendar creation directly through the MCP manager path, not through the normal owner approval/write gate path.

P2 - Google scope cache is stale across grant/revoke.
Evidence: `auth/google.py:145`, `auth/google.py:47`.

P2 - Google scopes are broader than least privilege.
Evidence: `scripts/auth.py:55`.

P2 - Google gating tests are list-based and can miss new write surfaces.
Evidence: `tests/test_google_workspace_send_policy.py`.

## Lane 3 - Apple / macOS

P1 - `note_create` can write to iCloud Notes without a real approval.
Evidence: `config/tools.yaml:661`, `tools/apple_notes/create.py:39`, `tests/test_apple_notes.py:167`.

P1 - First-run TCC/EventKit failures degrade silently or retry indefinitely.
Evidence: `README.md:630`, `tools/apple_notes/create.py:139`, `tests/test_apple_notes.py:335`, `agents/proactive.py:477`, `tools/reminders/create.py:173`.

P1 - Snoozed Apple reminders are recreated, not updated.
Evidence: `tools/reminders/snooze.py:35`, `tools/reminders/sync_apple.py:72`.

P1 - Recurring reminders do not keep Apple mirrors in sync.
Evidence: `agents/proactive.py:439`, `agents/proactive.py:467`.

P2 - Apple approval docs/config are contradictory.
Evidence: `config/tools.yaml:2768`, `tools/gatekeeper_can_use_tool.py:286`, `tests/test_destructive_tool_gating.py:101`.

P2 - Apple Notes read/search has a broad privacy and payload blast radius.
Evidence: `tools/apple_notes/search.py:50`, `tools/apple_notes/read.py:82`.

## Lane 4 - Backup / Restore / Disaster Recovery

P1 - Backups can be partially successful.
Evidence: `scripts/backup.sh:113`, `scripts/backup.sh:138`. `.env`, secrets, keychain, and cloudflared appends are best-effort; verification mainly proves the DB copy.

P1 - Install checks public key, but successful restore needs private key.
Evidence: `scripts/install_backup.sh:18`, `scripts/backup.sh:144`, `scripts/age_keygen.sh:9`.

P1 - Backup source path can diverge from the real DB path.
Evidence: `storage/db.py:37`, `scripts/backup.sh:27`. `HIKARI_DB_PATH` is supported by runtime but backup hardcodes `data/hikari.db`.

P1 - Restore lacks archive allowlist, DB validation, and service-stop safety.
Evidence: `scripts/restore.sh:76`, `scripts/restore.sh:81`, `scripts/restore.sh:83`.

P1 - Backup tests are not fully hermetic.
Evidence: `scripts/backup.sh:27`, `tests/test_backup_encryption.py:139`, `tests/test_backup_atomicity.py:70`.

P2 - Freshness monitors trust mtime rather than decryptability/integrity.
Evidence: `scripts/backup.sh:60`, `scripts/dead_man.py:128`, `agents/health.py:173`.

P2 - Operator receipts are log-only and lack a manifest.
Evidence: `scripts/backup.sh:172`, `scripts/install_backup.sh:103`.

P2 - Restore leaves decrypted secrets behind.
Evidence: `scripts/restore.sh:13`, `scripts/restore.sh:106`.

P2 - Legacy plaintext backups can satisfy freshness checks.
Evidence: `scripts/dead_man.py:131`, `agents/health.py:178`.

## Lane 5 - SQLite / Migrations / Graph Consistency

P1 - Migration idempotency is not safe across concurrent processes.
Evidence: `storage/db.py:543`, `storage/db.py:618`, `storage/migrations.py:53`.

P1 - Foreign keys are declared but not enabled on pooled connections.
Evidence: `storage/db.py:586`, `tests/test_foreign_keys.py:1`, `tests/test_foreign_keys.py:128`.

P1 - `graph_outbox` has no claim/lease and can duplicate Graphiti delivery.
Evidence: `storage/graph.py:271`, `storage/db.py:5226`, `storage/db.py:5241`.

P1 - Backups do not include Kuzu graph state and restore does not force replay.
Evidence: `scripts/backup.sh:86`, `scripts/restore.sh:83`, `storage/graph.py:36`, `storage/db.py:5678`.

P2 - Bitemporal fact invariants are mostly app-only.
Evidence: `tests/test_schema_constraints.py:282`, `storage/db.py:2091`, `storage/db.py:2184`, `storage/db.py:5457`.

P2 - Repair/backfill paths can replay stale facts if active/invalid filters are missed.

P2 - Migrations with internal `conn.commit()` weaken savepoint atomicity.

P2 - Schema sentinel state is not keyed by DB path.

## Lane 6 - Scheduler / APScheduler Reliability

P1 - `recurrence_rule` reminders can duplicate after downtime.
Evidence: `agents/proactive.py:443`, `tools/reminders/recurrence.py:136`, `agents/proactive.py:171`.

P1 - Cron jobs have no persistent job state or boot catch-up.
Evidence: `agents/scheduler.py:168`, `agents/telegram_bridge.py:3420`.

P1 - Proactive reservation is not crash-safe after Telegram delivery.
Evidence: `agents/proactive_gate.py:210`, `agents/proactive_gate.py:247`, `storage/db.py:3376`, `agents/proactive_reaper.py:18`.

P1 - Dead-man DB freshness cookie is read/tested but no main-process writer was found.
Evidence: `scripts/dead_man.py:105`, `tests/test_dead_man_semantics.py:67`.

P2 - Reminder send failures retry every poll with no backoff.
Evidence: `agents/scheduler.py:181`, `agents/proactive.py:417`.

P2 - Monthly DB-heavy prune jobs both run at day 1 04:00.
Evidence: `agents/scheduler.py:247`, `agents/scheduler.py:405`.

P2 - Scheduler health only checks job count.
Evidence: `agents/health.py:87`.

P2 - `engagement_tick` has no outer timeout.
Evidence: `agents/scheduler.py:492`, `agents/scheduler.py:547`, `agents/scheduler.py:574`.

P2 - Silent-day date math uses OS local date rather than scheduler timezone.
Evidence: `agents/scheduler.py:92`, `agents/proactive_gate.py:50`.

## Lane 7 - Config / Policy Drift

P1 - `confirm_send` approvals can time out instead of resolving.
Evidence: `config/tools.yaml:2768`, `tools/gatekeeper_can_use_tool.py:286`, `tools/gatekeeper_can_use_tool.py:358`, `tools/approvals.py:183`. `confirm_send` rows are treated as gated by policy, but text resolution only wakes `gatekeeper` rows.

P1 - Approval timeout config is split.
Evidence: `config/engagement.yaml:67`, `tools/gatekeeper_can_use_tool.py:97`, `tools/approvals.py:65`. Visible `approvals.timeout_sec: 60` does not govern live gatekeeper deadlines.

P1 - Registry schema typos can fail open.
Evidence: `tools/_tools_yaml.py:301`, `tools/_tools_yaml.py:246`, `tools/gatekeeper_can_use_tool.py:274`.

P2 - Deleted prompt-injection config keys still have override semantics.
Evidence: `config/engagement.yaml:782`, `agents/injection_guard.py:69`, `agents/external_wrap_hook.py:220`, `tests/test_tools_yaml.py:188`.

P2 - Generated subagent policy omits wildcard-gated tools by construction.
Evidence: `scripts/regen_subagent_policy.py:68`, `config/tools.yaml:2936`.

P2 - Drift tests are weaker than their docstrings.
Evidence: `scripts/validate_tool_registry.py:4`, `scripts/validate_tool_registry.py:37`, `tests/test_subagent_prompt_policy_drift.py:50`, `tests/test_subagent_prompt_policy_drift.py:182`.

## Lane 8 - Photo / Media Privacy

P1 - Automatic EXIF location processing leaks sensitive location context.
Evidence: `agents/telegram_bridge.py:1607`, `agents/telegram_bridge.py:1413`, `agents/telegram_bridge.py:1418`, `agents/telegram_bridge.py:1420`, `agents/proactive.py:552`.

P1 - Inbound photo routing sends full user photos to Anthropic before an explicit privacy gate.
Evidence: `agents/telegram_bridge.py:953`, `tools/photos/classify.py:145`. `photo_in.enabled` exists but is not read.

P1 - Media outbox drains to the current chat, not the row's intended chat.
Evidence: `storage/db.py:5350`, `agents/telegram_bridge.py:453`, `tools/photos/generate.py:81`.

P1 - Post-send DB failures can cause duplicate media sends.
Evidence: `agents/telegram_bridge.py:260`, `agents/telegram_bridge.py:287`, `agents/messaging.py:157`.

P1 - Generated-photo consent is model-asserted, not runtime-enforced.
Evidence: `tools/photos/generate.py:45`, `agents/runtime.py:1124`, `assets/APPEARANCE.md:32`.

P2 - Raw inbound media is retained without a media-specific TTL.
Evidence: `agents/telegram_bridge.py:905`, `agents/telegram_bridge.py:1615`.

P2 - Selfie and scene photo caps are split despite comments implying a shared pool.
Evidence: `tools/photos/_shared.py:86`, `tools/photos/scene.py:79`.

P2 - `scene_photo.enabled` and `photo_in.*` config are dead switches.
Evidence: `config/engagement.yaml:755`.

P2 - Sticker selection sends chat snippets to OpenRouter for a low-value decision.
Evidence: `agents/stickers.py:152`, `agents/runtime.py:415`.

P2 - Random sticker fallback can be emotionally wrong and bypass normal controls.
Evidence: `agents/telegram_bridge.py:583`, `agents/stickers.py:192`.

P2 - OpenRouter returned image URLs are fetched without host allowlisting.
Evidence: `tools/photos/_shared.py:127`.

## Lane 9 - Attachment / File Ingest

P1 - Native PDF/image blocks bypass the strong untrusted-content wrapper.
Evidence: `agents/telegram_bridge.py:1478`, `agents/telegram_bridge.py:1518`, `agents/telegram_bridge.py:1549`, `agents/telegram_bridge.py:1569`.

P1 - Telegram photo ingest likely does not provide real vision through `read_attachment`.
Evidence: `agents/telegram_bridge.py:942`, `tools/attachments/read.py:73`, `tools/_response.py:38`.

P1 - Unknown `image/*` MIME types fail open.
Evidence: `agents/telegram_bridge.py:1453`, `agents/telegram_bridge.py:1493`.

P1 - Size limits are checked before download/base64 expansion; missing Telegram `file_size` bypasses precheck.
Evidence: `agents/telegram_bridge.py:1593`, `agents/telegram_bridge.py:1596`, `agents/telegram_bridge.py:1471`, `agents/telegram_bridge.py:1516`.

P1 - Original filenames are used raw in model-facing text.
Evidence: `agents/telegram_bridge.py:1618`, `agents/telegram_bridge.py:1486`, `agents/telegram_bridge.py:1552`, `agents/telegram_bridge.py:1674`.

P2 - Unsupported/binary files can still be read after save.
Evidence: `agents/telegram_bridge.py:1577`, `tools/attachments/read.py:81`.

P2 - HTML extraction includes script/style text.
Evidence: `agents/telegram_bridge.py:1533`, `tests/test_file_ingest_html.py:17`.

P2 - Traversal tests do not cover symlink/TOCTOU.
Evidence: `tools/attachments/read.py:48`, `tests/test_read_attachment_path_validation.py:12`.

P2 - Privacy is partial: data is gitignored, but inbound photos/docs, captions, and EXIF labels persist.
Evidence: `.gitignore:7`, `agents/telegram_bridge.py:1683`, `agents/telegram_bridge.py:1419`.

## Lane 10 - Cost / Budget / Quota

P1 - Default persistent SDK path makes per-call budgets inert.
Evidence: `agents/runtime.py:909`, `agents/sdk_pool.py:83`, `agents/runtime.py:1028`, `agents/runtime.py:1148`.

P1 - Research worker is an unmetered scheduled Claude background path.
Evidence: `agents/subagents/research_worker.py:87`, `agents/subagents/research_worker.py:100`, `agents/scheduler.py:442`.

P1 - OpenRouter aux calls are mostly invisible to cost tracking.
Evidence: `agents/runtime.py:373`, `agents/runtime.py:456`, `agents/drift_judge.py:127`, `config/engagement.yaml:620`.

P1 - Eval cost cap does not cover rubric judge spend.
Evidence: `evals/conversation/runner.py:204`, `evals/conversation/runner.py:215`, `evals/conversation/runner_layer_c.py:129`.

P2 - Daily budget/cockpit status is split between old and new accounting.
Evidence: `tools/budget.py:34`, `agents/cockpit.py:477`, `storage/db.py:3214`.

P2 - Unknown models are stored as zero-cost rows.
Evidence: `agents/runtime.py:226`, `agents/runtime.py:238`, `storage/db.py:1505`.

P2 - Cache accounting loses audit detail.
Evidence: `storage/db.py:1505`.

P2 - Image, scene photo, and direct vision classifier costs are not tracked.
Evidence: `tools/photos/_shared.py:107`, `tools/photos/classify.py:134`.

P2 - Action reminder total cap excludes final summary cost.
Evidence: `tools/reminders/create.py:133`, `agents/proactive.py:352`.

## Lane 11 - Concurrency / Locks

P1 - Persistent SDK retry can leave a poisoned live client after the second failure.
Evidence: `agents/runtime.py:845`, `agents/sdk_pool.py:222`.

P1 - Compound-turn child ContextVars do not propagate back to the parent filter path.
Evidence: `agents/compound_turn.py:346`, `agents/compound_turn.py:141`, `agents/post_filter.py:228`.

P1 - `media_outbox` drains can double-send the same pending row.
Evidence: `agents/telegram_bridge.py:453`, `agents/scheduler.py:608`, `storage/db.py:5350`.

P1 - Direct MCP sessions have no per-call timeout and stale sessions survive TTL/error.
Evidence: `agents/mcp_manager.py:226`, `agents/mcp_manager.py:230`, `agents/mcp_manager.py:186`.

P2 - Async subprocess cleanup is uneven.
Evidence: `tools/voice_outbound.py:118`, `tools/apple_notes/_shared.py:72`, `tools/mcp_introspect.py:69`.

P2 - Turn ContextVars are set but usually not reset.
Evidence: `agents/runtime.py:1023`, `agents/runtime.py:1157`, `agents/runtime.py:1024`, `agents/_turn_state.py:15`.

P2 - Scheduler starts before the persistent SDK pool.
Evidence: `agents/telegram_bridge.py:3420`, `agents/telegram_bridge.py:3517`.

## Lane 12 - Prompt / Context Construction

P1 - `# gap_since_last` is likely broken on real text turns.
Evidence: `agents/runtime.py:1209`, `agents/hooks.py:1029`.

P1 - Session handoff replays raw prior content into prompt context.
Evidence: `agents/handoff.py:61`, `agents/handoff.py:115`, `agents/hooks.py:520`.

P1 - High-priority memory has sanitizer gaps.
Evidence: `agents/hooks.py:283`, `agents/reflection_sanitize.py:233`, `agents/peer_model.py:98`, `agents/reflection.py:447`.

P2 - Some candidate blocks mutate state before token culling decides inclusion.
Evidence: `agents/hooks.py:340`, `agents/handoff.py:101`, `agents/hooks.py:1074`.

P2 - Always-on context can exceed the configured context cap by design.
Evidence: `agents/hooks.py:745`, `agents/hooks.py:1077`, `tests/test_inject_memory_cull.py:119`.

P2 - Tool inventory wording can induce overconfidence.
Evidence: `agents/tool_inventory.py:117`, `tests/test_tool_inventory.py:54`.

P2 - Comfort mode loses one turn before rendering.
Evidence: `agents/hooks.py:1023`, `agents/mode_dispatch.py:53`.

## Lane 13 - Deployment / Ops Runbook

P1 - `.env` recovery documents the wrong Telegram token variable.
Evidence: `README.md:414`, `README.md:417`, `agents/telegram_bridge.py:3273`, `agents/telegram_bridge.py:3275`. The recovery snippet uses `HIKARI_BOT_TOKEN`, but the bot requires `TELEGRAM_BOT_TOKEN`.

P1 - Graph outbox recovery references a missing drain script.
Evidence: `README.md:397`, `README.md:398`. Read-only command `rg --files scripts | rg 'drain|outbox'` returned only `scripts/backfill_graph_outbox.py`, so `uv run python -m scripts.drain_outbox` will fail during incident recovery.

P2 - README health thresholds are stale.
Evidence: `README.md:272`, `README.md:274`, `agents/health.py:41`, `agents/health.py:44`. README says graph outbox degrades at `< 50` and recent errors at `<= 5`; code uses pending `> 10` and errors `> 10`.

P2 - Keychain migration docs imply broader daemon readiness than runtime supports.
Evidence: `README.md:27`, `scripts/migrate_secrets_to_keychain.py:128`, `scripts/migrate_secrets_to_keychain.py:133`, `agents/runtime.py:477`, `agents/runtime.py:495`, `agents/runtime.py:513`, `agents/runtime.py:537`, `agents/telegram_bridge.py:3273`. Runtime injects Google/Notion/GitHub tokens from keychain, but Telegram and Claude OAuth still need process env/dotenv wiring.

P2 - Health check backup freshness can accept legacy plaintext `.db` backups.
Evidence: `agents/health.py:178`. This repeats the backup lane's integrity concern in the visible startup health surface.

## Lane 14 - User Controls / Docs / Reversibility

P1 - `/settings set proactive.enabled false` is not a global proactive-off switch.
Evidence: `agents/cockpit.py:128`, `agents/cockpit.py:132`, `agents/cockpit.py:137`, `agents/scheduler.py:487`, `agents/scheduler.py:502`, `agents/morning_brief.py:259`, `agents/morning_brief.py:263`, `agents/morning_brief.py:311`, `agents/decision_log.py:46`, `agents/decision_log.py:49`, `agents/proactive_gate.py:227`. The setting only empties engagement producer sources; ceremony/background sends still rely on their own config plus `reserve_and_send()`.

P1 - `/status` can report pending approvals that `/approvals` cannot list.
Evidence: `agents/cockpit.py:460`, `agents/cockpit.py:468`, `agents/telegram_bridge.py:2244`, `agents/telegram_bridge.py:2248`, `tools/approvals.py:183`, `tools/approvals.py:195`. Status counts all pending rows, `/approvals` filters to `gate_kind='gatekeeper'`, and non-gatekeeper rows can be consumed without resolution.

P2 - Telegram command docs and autocomplete/menu source are contradictory.
Evidence: `README.md:474`, `README.md:500`, `README.md:504`, `agents/cockpit.py:32`, `agents/cockpit.py:49`, `agents/telegram_bridge.py:3287`, `agents/telegram_bridge.py:3291`, `tests/test_telegram_cockpit_cmds.py:878`. README says the autocomplete menu is sourced from `_COMMANDS` but lists commands intentionally removed from `_COMMANDS`.

P2 - `/reminders` says paginated, but the handler shows only the first 15 with no nav.
Evidence: `agents/cockpit.py:39`, `agents/telegram_bridge.py:2893`, `agents/telegram_bridge.py:2900`, `agents/telegram_bridge.py:2914`, `agents/cockpit.py:1518`, `agents/cockpit.py:1564`.

P2 - `/receipt` builds filter buttons but the slash handler discards them.
Evidence: `agents/telegram_bridge.py:2853`, `agents/telegram_bridge.py:2861`, `agents/telegram_bridge.py:2862`, `agents/cockpit.py:1373`, `agents/cockpit.py:1382`.

P2 - `/memorydump` pagination callback is off-by-one and drops the keyboard.
Evidence: `agents/cockpit.py:1209`, `agents/cockpit.py:1211`, `agents/telegram_bridge.py:2663`, `agents/telegram_bridge.py:2670`, `agents/telegram_bridge.py:2685`.

P2 - `/proactive snooze` accepts unknown sources.
Evidence: `agents/telegram_bridge.py:2309`, `agents/telegram_bridge.py:2315`, `agents/cockpit.py:1122`, `agents/cockpit.py:1138`, compared with `/proactive on|off` validation at `agents/telegram_bridge.py:2343`, `agents/telegram_bridge.py:2346`.

P2 - `/settings set proactive.enabled` accepts invalid JSON source lists and scheduler silently ignores unknown IDs.
Evidence: `agents/cockpit.py:260`, `agents/cockpit.py:262`, `agents/cockpit.py:140`, `agents/cockpit.py:141`, `agents/scheduler.py:502`, `agents/scheduler.py:505`.

P2 - `/settings` persistence semantics are mixed.
Evidence: `agents/cockpit.py:72`, `agents/cockpit.py:81`, `agents/cockpit.py:82`, `agents/scheduler.py:600`, `agents/scheduler.py:602`. `GRAPHITI_ENABLED` writes runtime state and env, but the reader and scheduler start path read env only after restart.

P2 - Privacy/media toggles are not exposed in the user control surface.
Evidence: `agents/cockpit.py:238`, `agents/cockpit.py:302`, `config/engagement.yaml:755`. The visible settings allowlist has no media ingestion, EXIF, geocoding, voice retention, or raw media TTL controls.

## Lane 15 - End-to-End Red-Team Synthesis

P1 - A third-party-content-to-tool-action chain still has multiple weak joins.
Evidence chain: untrusted file/native blocks can enter prompts without uniform wrapping (`agents/telegram_bridge.py:1478`, `agents/telegram_bridge.py:1549`), stored handoff/context can later replay raw text (`agents/handoff.py:115`, `agents/hooks.py:520`), registry typos can fail open (`tools/_tools_yaml.py:301`, `tools/gatekeeper_can_use_tool.py:274`), and approval visibility is split (`agents/cockpit.py:460`, `agents/telegram_bridge.py:2248`). Typed confirmation is a strong barrier, but the surrounding surfaces can mislead the owner about what is pending and why.

P1 - Media privacy can cross local, model, external geocoder, and proactive memory boundaries without a single explicit consent ledger.
Evidence chain: inbound photo classifier sends bytes to Anthropic (`tools/photos/classify.py:145`), EXIF GPS can be reverse-geocoded (`agents/telegram_bridge.py:1418`), location labels can persist (`agents/telegram_bridge.py:1420`), and recurring-location proactive logic can later use that context (`agents/proactive.py:552`).

P1 - A "turn off proactives" expectation can be violated by separate scheduled ceremony paths.
Evidence chain: `proactive.enabled=false` stores `[]` (`agents/cockpit.py:137`), unified engagement tick then has no producer tasks (`agents/scheduler.py:509`), but morning brief and decision resolver still call `reserve_and_send()` (`agents/morning_brief.py:313`, `agents/decision_log.py:70`), whose final gate has no global enabled-source check (`agents/proactive_gate.py:227`).

P2 - Tests prove many happy paths but not the cross-boundary failures.
Evidence: control tests passed (`111 passed`), prompt/context tests passed (`38 passed`), and concurrency smoke tests passed (`36 passed`), but current assertions do not cover invalid proactive source lists, hidden approval rows, post-send DB failure duplicates, or handoff/header-forgery replay.
