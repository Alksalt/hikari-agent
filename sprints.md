# Hikari — Post-Review Sprints (2026-05-29)

Source of truth: `codex/hikari-agent-review-FRESH-2026-05-29.md` (139 detailed findings).
Prior `2026-05-28` review docs are superseded → `codex/archive/`.

**Scope:** all open **P1** + the **P2s that are control-plane lies / data-integrity / security** + a few cheap folded **P3s**. Pure cosmetic / dead-code P3s are deferred (see appendix).

**Out of scope by decision** (single-user personal bot — see `alt-wiki/projects/hikari-agent/DECISIONS.md`): crisis/self-harm override, are-you-real/AI-disclosure truth boundary, distress carve-outs, anti-dependency persona edits, and their eval coverage. These were the only remaining P0 — **no P0 remains.**

**Already shipped 2026-05-29 (disposed):** Kuzu embedding double-nest, proactive.enabled global enforcement, mark_consumed re-fire, apple_events→gatekeeper, skill_approve gate, child-logger scrub, compound aggregation, corrected-fact embedding. `run_skill` stays ungated by decision; do not re-add a gate.

---

## How to run

Three **file-isolated** sprints — no two sprints edit the same file, so all three run as parallel agents (separate worktrees) with zero merge conflicts. Sprint 3 is the largest (the interconnected agent core can't be split without sharing `telegram_bridge.py` / `engagement.yaml` / `storage/db.py`); it's internally phased.

**Per-sprint loop:** implement → deslop → `blast-radius-mapper` → 3 parallel reviewers (correctness/security/integration) → commit per green phase. Implementer subagents must **not** commit (one auto-committed a broken state before). Models: Opus for architects/correctness/security/blast-radius, Sonnet for implementers/integration.

**Quality gates (all sprints, every phase):**
```
uv run pytest -q                  # background it (~6.5 min); read the summary line, never pipe to tail
uv run python scripts/validate_tool_registry.py
uv run python scripts/validate_mcp_servers.py --skip apple_events,apple_shortcuts --allow-unreachable duckdb,github,playwright
```
Any schema-changing merge → `launchctl` restart + tail the err log (test DBs are always fresh, so migration-ordering bugs only show in prod).

### Cross-sprint contracts (4 interfaces, no shared files)
1. **Autonomous-action** — S3 adds `sdk_pool.set_autonomous_window(bool)` / `in_autonomous_window()` and sets it inside `_RUN_LOCK` in `runtime.py`; **S1** changes `gatekeeper_can_use_tool.py:322` to read `sdk_pool.in_autonomous_window()` instead of the boot-snapshotted ContextVar. (S1 imports `sdk_pool`, never edits it.)
2. **`/register` hardening** — **S1** adds the IP rate-limiter in `oauth.py`; **S3** adds the row-count ceiling in `db.oauth_client_register`.
3. **Cost helpers** — `runtime._log_aux_cost` / `runtime._record_llm_cost` already exist; **S1** (photo classifier) and **S2** (bounded_rewrite) call them. S3 owns `runtime.py`.
4. **UTC logger** — both entrypoints set `logging.Formatter.converter = time.gmtime`: **S3** in `telegram_bridge.main()`, **S1** in `mcp_external/launch.py` (different files).

---

## SPRINT 1 — External Surface & Tool Governance (~24)

**Owns:** `mcp_external/{server,oauth,launch}.py` · `auth/{google,store}.py` · `scripts/{auth,validate_mcp_servers,validate_tool_registry}.py` · `config/tools.yaml` · `tools/_tools_yaml.py` · `tools/gatekeeper.py` · `tools/gatekeeper_can_use_tool.py` · `tools/skills/core.py` · `tools/photos/{_shared,scene,classify}.py` · `tools/reminders/{sync_gcal,sync_apple}.py` · `agents/injection_guard.py` · `agents/external_wrap_hook.py` · `agents/mcp_manager.py` · `agents/skill_promoter.py`

### P1
- **allowed_hosts reads wrong key → every external MCP call 421s.** `mcp_external/server.py:143-159`. Fix: resolve `public_base_url_env` (PUBLIC_BASE_URL) with the same precedence as `oauth.py`, then extend allowed_hosts/origins; factor a shared resolver used by server.py/oauth.py/launch.py.
- **Passphrase limiter keys on 127.0.0.1 (tunnel) → global + self-DoS.** `oauth.py:134-136,476-482`. Fix: read `CF-Connecting-IP` then left-most `X-Forwarded-For` when `behind_tls_proxy`, gated on a new `mcp_external.trusted_forwarded_ip`; use resolved IP for limiter key + audit.
- **Open `/register` unbounded (rate-limit half).** `oauth.py:281-314`, `launch.py:130-134`. Fix: IP-keyed RateLimiter (`register_max_attempts` 10 / `register_window_seconds` 3600) after the forwarded-IP fix. *(row-count ceiling → S3 contract #2.)*
- **Direct MCP call never evicts poisoned session, no per-call timeout.** `agents/mcp_manager.py:204-245`. Fix: `asyncio.wait_for(call_tool, timeout=per-server (30s))`; on error/timeout tear down the session under `handle._lock` (aclose exit_stack, `session=None`) then raise `McpCallError`.
- **No enum validation of gate/access_mode at registry load — typo fails OPEN.** `tools/_tools_yaml.py:301-327`. Fix: raise if `gate ∉ {None,gatekeeper,confirm_send}` or `access_mode ∉ {None,read,write,destructive}`; module-constant sets.
- **Scope cache not cleared on revoke.** `auth/google.py:232-249,148-154`. Fix: in `revoke()` after `_store.clear('google')` set `auth.google.scopes`/`scopes_checked_at` = None; same in `write_grant_to_keychain()`.
- **KeychainStore.clear('google') leaves the `grant` blob.** `auth/store.py:77-82`. Fix: add `'grant'` to the cleared-keys tuple.
- **Snooze creates duplicate orphaned GCal events.** `tools/reminders/sync_gcal.py:91-110`. Fix: look up `reminder_get`; if `gcal_event_id` set → `update_calendar_event(event_id=…)`, else create. Mirror in `sync_apple.py`.

### P2
- **Canary exfil hard-deny scans only top-level scalars — nested bypass.** `agents/injection_guard.py:170-172` + `gatekeeper_can_use_tool.py:130`. Fix: lift `_walk_strings` into `injection_guard`, build blob via deep-walk before `outbound_contains_canary`; share both call sites.
- **External-wrap PostToolUse fails OPEN on wrap exception.** `agents/external_wrap_hook.py:256-263`. Fix: fail CLOSED — return `updatedToolOutput` with a `[suppressed: wrap failed]` placeholder, never `{}`.
- **`hikari_utility` WRITE wildcard fails OPEN (read wildcard).** `config/tools.yaml:1000-1012` + `validate_tool_registry.py:78-82`. Fix: set wildcard `access_mode:write`; remove the `mcp__hikari_utility__` exemption in the validator.
- **Invalid resource indicator mints unusable (no-aud) token then 401s.** `oauth.py:378-389,402/528` + `launch.py:178-194`. Fix: bind `aud` to server base URL when resource None; return `invalid_target` on bad resource; structured 401 body.
- **validate_mcp_servers substring soft-pass hides real breakage.** `scripts/validate_mcp_servers.py:59,74-82`. Fix: typed `McpInitializeTimeout` vs `McpProtocolError`; soft-skip only the timeout / allow-unreachable.
- **External read tools pass `limit` through with no clamp — read amplification.** `mcp_external/server.py:162-263`. Fix: `_MAX_LIMIT=cfg('mcp_external.max_read_limit',50)`; `limit=max(1,min(int(limit)or default,_MAX_LIMIT))` in recall/observations/wiki_search/lexicon_top; reject non-int.
- **Apple/Playwright/YouTube/DuckDB wildcard access_modes inconsistent — future writes fail OPEN.** `config/tools.yaml:2922-2987`. Fix: `access_mode:write` on playwright/youtube/duckdb wildcards; correct the apple_events comment (unlisted = denied).
- **`summarize()` prefix fallbacks hide write payload from owner.** `tools/gatekeeper.py:464-491`. Fix: make the github/notion/google prefix branches `raise NotImplementedError` so the critical-field renderer shows body/content/files.
- **`note_create` gate:null — ungated iCloud Notes write.** `config/tools.yaml:664`. Fix: `gate:confirm_send`; drop the redundant `confirm` param in `apple_notes/create.py`.
- **`_call_flux` provider image URL fetched without host allowlist — SSRF.** `tools/photos/_shared.py:127-130`. Fix: require `https` + hostname ∈ openrouter cdn before fetch; ValueError otherwise.
- **scene_photo + generate_photo split daily caps — combined > budget.** `tools/photos/scene.py:79-92` + `_shared.py:86-98`. Fix: scene calls the `_shared` counter funcs (one shared counter).
- **`_google_status` shows expires_at as granted_at + requested-not-granted scopes.** `scripts/auth.py:127,108`. Fix: write `granted_at` at grant time; label scopes as requested-at-grant.
- **Over-broad base Google scopes (full mailbox always).** `scripts/auth.py:55-63`. Fix: drop redundant `gmail.modify`; make mail scopes opt-in (`--add`) or gate on enabled gmail tools.
- **Photo classifier (Sonnet vision) cost never tracked.** `tools/photos/classify.py:178-183`. Fix: return `(text, body)`, extract `usage`, call `runtime._log_aux_cost(path='photo_classify')` in try/except.

### P3 (cheap, security-adjacent)
- **skill_promoter stages unvalidated skill_id.** `agents/skill_promoter.py:127-146`. Fix: call `_validate_skill_id` before the `session_scratch` INSERT.
- **Daily-cap non-atomic read-modify-write (photos).** `tools/photos/_shared.py:93-98`, `scene.py:86-92`. Fix: `db.runtime_increment('photos_sent_today')`; keep date-rollover guard. *(voice_outbound half → S3.)*
- **Non-deterministic `tool_use_id` fallback breaks idempotency.** `gatekeeper_can_use_tool.py:289`. Fix: deterministic `'synth-'+sha256(tool_name+json.dumps(input,sort_keys=True))[:24]`.
- **UTC logger** (contract #4): add `logging.Formatter.converter = time.gmtime` in `mcp_external/launch.py`.

---

## SPRINT 2 — Persona, Voice & Eval Integrity (~18)

**Owns:** `assets/{PERSONA,APPEARANCE}.md` · `.claude/skills/character-voice/*` · `config/hikari_playlist.yaml` · `agents/post_filter.py` · `agents/belief_frame.py` · `agents/dialectic.py` · `agents/tonal_recall.py` · `evals/*` · `tests/persona/*` · `tests/test_lore_dormant_schema.py`

### P1
- **INTIMATE.md "never gated by trust stage" overrides PERSONA stage gates.** `SKILL.md:12`. Fix: state it's gated by `relationship_stage` AND mood (inversion + direct vulnerability 5+; core-wound + i-love-you 7); add stage-gate headers inside INTIMATE.md sections. *(persona-arc integrity, not safety.)*
- **SKILL.md load contract points at orphaned LORE.md; dormant gate bypassed.** `SKILL.md:3,13`. Fix: point at `LORE_CORE.md`; document `LORE_DORMANT.md` (gated, one/session); delete orphaned `LORE.md`; update `tests/test_smoke.py` + `test_voice.py` refs.
- **Anti-sycophancy golden case judged by voice_drift rubric that never scores capitulation.** `evals/.../anchor_rebuttal_antisycophancy.yaml` + `judge.py:66-67` + `rubrics.yaml:64-94`. Fix: add a `no_capitulation/anchor_hold` criterion to voice_drift criteria, OR re-route through `rubric_judge` weighting `epistemic_independence`+`voice_integrity`.
- **Trailing task-question gate bypassed by trailing emoji/quote.** `agents/post_filter.py:908-910`. Fix: strip trailing in-character emoji + quotes/brackets before `endswith('?')`; regression test.
- **[eval] rubric pass threshold 0.6 on a 0–4 scale — gate doesn't gate.** all 10 `rubric_*.yaml:5`; `runner_layer_c.py:103-104,153`. Fix: `weighted_avg >= 3.0` in all 10 rubric YAMLs.
- **[eval] rubric_judge scores author-written YAML, not live output.** `runner_layer_c.py:80-170`. Fix: add `kind:rubric_live` calling `run_user_turn`; until then rename to `kind:judge_calibration` + runner comment.
- **[eval] Malformed-JSON LLM reply crashes the whole run.** `scorer.py:122`, `judge.py:103`, `runner_layer_c.py:136`. Fix: wrap `json.loads` → `raise RuntimeError(...) from exc` so the per-case guard records one failure and continues.

### P2
- **rewrite_or_fallback returns raw rewrite, not markdown-stripped second pass.** `post_filter.py:879-895`. Fix: `return second.text` at :895.
- **Markdown strip mangles fenced code blocks into stray double-backticks.** `post_filter.py:124,156`. Fix: multiline fence regex substituted ahead of `_MD_CODE_RE.sub`.
- **PLAYLIST Youth/Daughter freely surfaceable, bypasses its own ask-twice gate.** `PLAYLIST.md:18`. Fix: remove from free-surface table / add dormant guard; mirror `hikari_playlist.yaml` comment.
- **Dormant-lore gates are model-trust only; test validates schema not behavior (control-plane lie).** `LORE_DORMANT.md:1-18` + `test_buried_lore_gate.py`. Fix (honesty, minimal): state in SKILL.md these are model-discretion heuristics with no runtime enforcement; rename test → `test_lore_dormant_schema.py`.
- **SKILL.md load contract omits DAILY_LIFE/TOPIC_RULES/PLAYLIST.** `SKILL.md:10-15`. Fix: add load bullets + triggers; cross-ref PERSONA.
- **belief_frame IDENTITY_CLAIM_RE matches benign negations as 90-day beliefs.** `belief_frame.py:56-58`. Fix: require an identity/category verb or capitalized object after the negation.
- **dialectic fence-strip uses fragile split, discards all insights on stray backticks.** `dialectic.py:65-69`. Fix: use the splitlines fence helper (as drift_judge/reflection).
- **tonal_recall `UPDATE WHERE id=1` silently succeeds on 0 rows.** `tonal_recall.py:90-94`. Fix: check `rowcount`, warn on 0.
- **[eval] Missing OPENROUTER_API_KEY makes rubric cases FAIL, not skip.** `runner_layer_c.py:88-96`, `runner.py:220-223`. Fix: treat `kind=='skipped'` as excluded from pass/fail.
- **bounded_rewrite Sonnet turn records no cost (cost branch).** `post_filter.py:806-828`. Fix: add `elif isinstance(msg, ResultMessage): runtime._record_llm_cost(path='bounded_rewrite', …)`. *(engagement.yaml Haiku→Sonnet comment/budget → S3.)*

### P3 (cheap, eval reliability)
- **banned_phrases substring match → false positives on legit replies.** `banned_phrases.py:33`. Fix: end-anchored regex for task-tail entries; `find_banned` accepts literals + compiled regex.
- **Slow sycophancy eval hard-codes claude-haiku-4-5 (violates Never-Haiku).** `tests/persona/test_sycophancy.py:100`. Fix: `claude-sonnet-4-6`, lower budget, assert no `haiku`.

---

## SPRINT 3 — Agent Core: Runtime · Memory · Delivery · Cost · Durability (~35, phased)

**Owns:** `agents/runtime.py` · `reflection.py` · `hooks.py` · `mode_dispatch.py` · `stickers.py` · `callback_surface.py` · `compound_turn.py` · `sdk_pool.py` · `drift_canary.py` · `log_scrub.py` · `health.py` · `scheduler.py` · `proactive.py` · `proactive_gate.py` · `proactive_reaper.py` · `decision_log.py` · `morning_brief.py` · `future_letter.py` · `telegram_bridge.py` · `engagement/{sender,selector,guard,cadence,producers/*}` · `storage/{db,migrations,retrieval,graph}.py` · `tools/voice_outbound.py` · `tools/budget.py` · `tools/memory/{remember,correct_fact}.py` · `agents/subagents/research_worker.py` · `scripts/backup.sh` · `config/engagement.yaml` · `README.md` · `tests/test_proactive_*.py`

### Phase 3A — Storage & durability
- **[P1] OAuth tokens stored plaintext.** `db.py:4564-4630`. Fix: store `sha256` (`token_hash`), hash before `WHERE`, migration drops plaintext, `secrets.compare_digest`. Apply to validate/consume_refresh/revoke_family + parent_token.
- **[P1→contract #2] `/register` row-count ceiling.** `db.oauth_client_register`. Fix: `SELECT COUNT(*)` + raise above max (50); prune token-less clients in daily maintenance.
- **[P2] PRAGMA foreign_keys never on — CASCADE/REFERENCES unenforced in prod.** `db.py:606-612`. Fix: `PRAGMA foreign_keys=ON` in `_get_pooled_conn` (verify the 2 table-rebuild migrations still pass).
- **[P2] Migration checksum hashes full source — docstring edit bricks boot.** `migrations.py:30-31`. Fix: explicit `version`/tag per migration, compare the tag not `getsource()`.
- **[P3] No cross-process migration lock.** `db.py:619-632`. Fix: `BEGIN IMMEDIATE` before the migration cascade (or wrap every ALTER in dup-column try/except).
- **[P1] Kuzu graph never backed up.** `scripts/backup.sh:95-111`. Fix: checkpoint + `cp -R data/hikari.kuzu` into TMP, `tar --append`; extend verify to open the kuzu artifact.
- **[P2] ACT-R category decay dead for user/corrected facts (NULL → 29-day default).** `retrieval.py:82-148`, `remember.py:52`, `correct_fact.py:12`. Fix: thread `fact_category`; infer from predicate in `remember()`; carry over old category in `correct_fact`.
- **[P2] Graph read failure masked by DEBUG swallow, no source metric.** `recall.py:84-93`, `graph.py:326-328`. Fix: `recall_graph_fallback/hit` counters + `graph_search_error` bump; expose ratio in `health.py`.

### Phase 3B — Cost & observability
- **[P1] `_call_aux_llm` records no cost (~120 OpenRouter sites).** `runtime.py:461-475`. Fix: read `payload['usage']`, `_log_aux_cost(...)` in try/except — one edit covers all sites.
- **[P2] research_worker uncapped + uncosted.** `research_worker.py:88-106`. Fix: `max_budget_usd` from config; `_record_llm_cost` on ResultMessage.
- **[P2] `cost_today()` always 0.** `tools/budget.py:34-39`. Fix: live `SUM(cost_usd) FROM llm_costs WHERE ts>=midnight`; point cockpit at it.
- **[P2] ElevenLabs TTS rate missing → $0.** `voice_outbound.py:210-219`. Fix: add rate to `_MODEL_RATES_USD_PER_1M` or compute `len/1000*0.10`.
- **[P1] voice_outbound always resolves mood as 'focused' — irritable gate dead.** `voice_outbound.py:64`. Fix: `db.get_core_block('mood_today')`.
- **[P3] voice_outbound daily cap non-atomic.** `voice_outbound.py:77-83`. Fix: `db.runtime_increment`.
- **[P1] log_recent_errors compares local-time logs vs UTC cutoff — false-green.** `health.py:233-236` + UTC logger (contract #4) in `telegram_bridge.main()`.
- **[P2] CRITICAL canary lines invisible to health check.** `health.py:63`. Fix: regex `\b(ERROR|CRITICAL)\b`.
- **[P1] README health thresholds stale vs code.** `README.md:272-274`. Fix: match `health.py` constants (>10).
- **[P3] CanaryAlertFilter re-embeds the canary + sk- pattern ordering.** `log_scrub.py:24-26,100-104`. Fix: redact before re-emit; order `sk-ant-`/`sk-or-` before generic `sk-`.
- **[P3] No double-bill guard.** `telegram_bridge.py:3323-3346`. Fix: warn if `ANTHROPIC_API_KEY` and `CLAUDE_CODE_OAUTH_TOKEN` both set.

### Phase 3C — Modes, memory & runtime correctness
- **[P1→contract #1] Autonomous-action gate dead on the live persistent path.** `runtime.py:1150,1160`, `sdk_pool.py`. Fix: `sdk_pool.set_autonomous_window(True)` inside `_RUN_LOCK`, reset in finally; add `set/in_autonomous_window`. (S1 reads it.)
- **[P1] self_model written + re-injected with no sanitization.** `reflection.py:447-456`, `hooks.py:933-943`. Fix: `sanitize(kind='peer')` on write; defensive re-sanitize loop in `_format_self_model` (mirror `_format_peer_representation`).
- **[P1] comfort_mode loses one turn (decrement same turn as activation).** `hooks.py:1058-1062`. Fix: decrement post-turn (Stop/PostTool), or skip if `activated_at` within last N sec.
- **[P2] `clear_on_session_boundary()` never called — modes leak across sessions.** `mode_dispatch.py:149-154` + `runtime.py`. Fix: call it after `arm_if_heavy()`, before resetting session turn count.
- **[P2] belief-frame adversarial context dropped on compound turns.** `telegram_bridge.py:863-865`, `compound_turn.py:242-248`. Fix: add `internal_belief_context` param to `run_compound_turn_typed`, thread to `respond()`.
- **[P2] Compound child SDK-error string embedded in receipt past the guard.** `compound_turn.py:215-219,360-411`. Fix: `looks_like_sdk_error` before marking step 'done' → 'failed'.
- **[P2] session_id committed before send/persist confirmed.** `runtime.py:829,963`. Fix: defer `set_session_id` to the success branch after send `ok=True`.
- **[P2] compute_cycle_state naive `datetime.now()` for circadian phase.** `reflection.py:1366`. Fix: tz-aware `now` from `scheduler.timezone`.
- **[P2] their_model_of_me quarterly stamp skipped on exception → re-runs daily.** `reflection.py:459-479`. Fix: stamp in `finally`.
- **[P2] Sticker probability not scaled by warmth_multiplier.** `stickers.py:64-65`. Fix: multiply `_probability()` by warmth band factor (clamp 0–1). *(also closes the "commit 2245035 sticker lie" P3.)*
- **[P2] Warmth band thresholds hardcoded in hooks diverge from cadence.** `hooks.py:226`. Fix: `cfg.get('cycle_modulation.low_tolerance_below'/.open_at_or_above')`.
- **[P2] slow_burn_tell consumed at injection — payoff lost if model ignores hint.** `callback_surface.py:350-361`, `hooks.py:777-784`. Fix: defer consumption to confirmed emission (post-send substring/semantic match → `mark_slow_burn_surfaced`).
- **[P2] Sycophancy axis write-only; config claims an unwired weekly audit.** `reflection.py:539-552`, `drift_judge.py:220-243`, `engagement.yaml:679-681`. Fix: add `db.sycophancy_recent_count(...)` read in `run_daily_reflection`.
- **[P3] Canary answer text unescaped between delimiters — breakout.** `drift_canary.py:274,320`. Fix: `_escape_untrusted_markers` (or UNTRUSTED_SOURCE wrapper) before interpolation.

### Phase 3D — Delivery, cadence & bridge
- **[P1] agent_spontaneous cap unenforceable — sender always records user_anchored.** `engagement/sender.py:162-167`, `engagement.yaml:550-559`. Fix: pool-aware recorder (`record_spontaneous_sent`/`record_ceremony_sent`/`record_user_anchored_sent`); backfill `allowed_sources` to all 11 agent_spontaneous producers. *(consolidates the 3 duplicate findings.)*
- **[P1] Per-source snooze + "snooze all" no-op for ceremonies/reminders.** `proactive_gate.py:227-238`, `cockpit.py`, `selector.py`. Fix: read `proactive_snooze_until` in `reserve_and_send` final gate; add `snooze` AbortReason; validate source ids.
- **[P1] 5 world-delta producers permanently dead + no engagement config.** `engagement.yaml:39-44,1085-1244`. Fix: append the 5 to `default_enabled_sources` AND add `engagement.*` blocks (min_interval, value floor, interruption_right) **before** enabling.
- **[P1] reengage_silence + late_night_dissolution fire from session 1 (no stage gate).** `producers/reengage_silence.py`, `late_night_dissolution.py` + `engagement.yaml`. Fix: `min_stage` (default 6) read in `collect`; add `min_stage:6` to both source blocks.
- **[P2] just_got_home uses UTC hour.** `producers/just_got_home.py:66-67`. Fix: tz-aware local hour via `_resolve_local_tz_name`.
- **[P1] media_outbox no atomic claim — double-send.** `telegram_bridge.py:453-468` + `db.py` claim fn + status migration. Fix: claiming `UPDATE … SET status='sending' … RETURNING`; stale-'sending' reaper.
- **[P2] media_outbox drains to owner_id, ignoring per-row chat_id.** `telegram_bridge.py:453-468`. Fix: dispatchers honor `payload['chat_id']`.
- **[P2] Scheduler starts before sdk_pool.startup() — connection leak.** `telegram_bridge.py:3419,3516`, `sdk_pool` startup. Fix: guard in `startup()` if `_live.client` not None, or move `scheduler.start()` after startup.
- **[P2] Fast restart (<60s) leaves reserved rows → duplicate sends.** `proactive_reaper.py:15-24`. Fix: lower threshold ~10s or soft-dedup young reserved rows.
- **[P3] No periodic reserved-row cleanup.** `proactive_reaper.py` + `scheduler.py`. Fix: `IntervalTrigger(minutes=10)` reaper, ~300s grace.
- **[P3] memory_prune + monthly_prune both at day=1 04:00.** `scheduler.py:254-256,411-416`. Fix: `monthly_prune` minute=2.
- **[P1] photo_in.enabled config key never read.** `telegram_bridge.py:889-965,1397,1609`. Fix: guard `handle_photo` + `_try_ingest_document_photo` on `cfg.get('photo_in.enabled',True)`.
- **[P1] Inbound voice files never deleted.** `telegram_bridge.py:1010-1090`. Fix: `unlink(missing_ok=True)` after transcribe + on every early return (or weekly prune of `data/user_voice/`).
- **[P2] EXIF Nominatim label raw into the LLM prompt.** `telegram_bridge.py:1667,1676`. Fix: `injection_guard.wrap_untrusted('nominatim', label)`.
- **[P2] Morning brief injects HF paper titles unwrapped.** `morning_brief.py:221-231`. Fix: `wrap_untrusted('morning_brief:hf_paper_title', title)` per paper.
- **[P2] future_letter can half-send then permanently refuse the rest.** `future_letter.py:557,565,601-621`. Fix: single `reserve_and_send` (all-or-nothing) or resume-from-chunk index; don't UNIQUE-lock the row on partial failure.
- **[P2] scheduler_gate_enabled=false skips the whole noise floor.** `engagement/guard.py:25-26`. Fix: still run silent_day/quiet/silence; move any true dev bypass to an env var.
- **[P2] Cofire "hold #2 for 2h" is dead code (write-only).** `selector.py:259-274`. Fix: implement the held-candidate drain at tick start, OR delete the hold + docstring (honest skip).
- **[P2] Cofire state mutated at selection, before compose/guard/send.** `selector.py:307`. Fix: write `_set_cofire_state` only after `sender.send` returns a row id.
- **[P2] /diary + /receipt keyboards discarded, callbacks unregistered.** `telegram_bridge.py:2812,2861` + `_handle_callback`. Fix: build keyboards + add `diary`/`receipt` callback branches.
- **[P2] mem:page off-by-one + fact filter diverges from /memorydump.** `telegram_bridge.py:2663-2687`. Fix: use `cockpit.format_memorydump(page=0-based)`; same filter for initial + callback.
- **[P2] Forget/Pin inline buttons mutate facts with no confirmation.** `telegram_bridge.py:2631-2661`. Fix: confirm step (`mem:forget_confirm:{fid}`) or route through `/memory forget` friction.
- **[P3] cmd_silence ignores format_silence_ack (expiry hidden).** `telegram_bridge.py:1776`. Fix: send `cockpit.format_silence_ack(minutes)`.
- **[P2] decision_log calibration surface bypasses the proactive gate.** `decision_log.py:113`. Fix: `reserve_and_send(producer_id='decision_log', pattern='ceremony', dedup_key=…)`.
- **[P2] /settings proactive.enabled writer accepts arbitrary source ids.** `cockpit.py:138-141`. Fix: validate against `ALL_PRODUCER_IDS`, raise on unknown.
- **[P1] Recurring reminder GCal/Apple mirrors drift after every fire.** `proactive.py:448,259`. Fix: `db.reminder_requeue_sync(row['id'])` after `reminder_update_fire_at`.
- **[P3] 4 retention keys missing from yaml.** `engagement.yaml:1076-1078`. Fix: add `tool_calls_days/graph_outbox_sent_days/media_outbox_terminal_days/proactive_events_days`.
- **[P3] bounded_rewrite "Haiku" comments + under-budget.** `engagement.yaml:252,312,316,317`. Fix: comments → `claude-sonnet-4-6`; raise `rewrite_max_budget_usd` to 0.04.
- **[P3] Regression tests for proactive off-switch semantics.** `tests/test_proactive_*.py`. Fix: (a) `proactive_enabled_sources_override='[]'` → tick → assert zero sends; (b) corrupt JSON → falls back to DEFAULT (not empty / not exception) + `log.warning` in the except.

---

## Appendix — deferred (not scheduled)

Pure cosmetic / dead-code / no-functional-bug P3s, dropped from scope:
- `_ebbinghaus_multiplier` dead code (`retrieval.py:334-362`)
- `judge_prompt_template` dead block (`rubrics.yaml:100-114`)
- `budget.call_window_*` dead config keys (`engagement.yaml:104-105`)
- `scene_photo._DAILY_CAP` stale description (`scene.py:109`)
- `current_comfort_mode()` getter side-effect (`mode_dispatch.py:87-89`)
- Reflection import-time constants frozen vs cockpit reload (`reflection.py:1023,1646,1648`)
- `task_solicit_cues` per-call compile (`post_filter.py:919-923`)
- `_cb_rem` snooze float — dead until `format_reminders_page` wired (`telegram_bridge.py:2732`)
- `query.answer()` before owner gate — single-owner DM (`telegram_bridge.py:2754-2755`)
- `should_wake()` ignores `source_id` — no functional bug (`guard.py:13-63`)
- Poisoned live-client cached after 2nd failure (`runtime.py:850-879`)
- Canary probe reveals "this is a probe" framing (`drift_canary.py:353-359`)

Out-of-scope by decision (single-user): crisis/self-harm override, are-you-real truth boundary, distress carve-outs, anti-dependency persona edits, and their eval/test coverage.
