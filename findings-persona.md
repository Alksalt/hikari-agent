# Hikari Upgrade Findings — Persona, Engagement, Intelligence

Synthesized from 10 web-research agents + 3 internal codebase explorers + 8 verification agents (2026-05-27).
Each item: **Why** (cited source), **Fix** (concrete edit), **Status** (verified against current codebase).

## Verification summary

| Status | Count | Items |
|---|---|---|
| EXISTS | 1 | 16a (single-pass extraction) |
| PARTIAL | 12 | 1, 3, 6, 8, 9, 10, 11, 12, 15, 16b, 18, 21, 22 |
| MISSING | 17 | 2, 4, 5, 7, 13, 14, 17, 19, 20, 23, 24, 25, 26, 27, 28, 29, 30 |

**Key corrections from verification:**
- Photo provider is **FLUX.2-Klein** (via OpenRouter), not NanoBanana as initial mapping said. `tools/photos/_shared.py:37-38`.
- **Readwise was deliberately removed 2026-05-21** with a `Do NOT re-add` comment in `agents/engagement/producers/readwise_daily_review.py`. Don't propose re-adding until Readwise ships a hosted HTTP MCP.
- Prompt caching: architecture is cache-aware (the comment in `runtime.py:439-440` proves it), but `cache_control` param is missing on the system block. **Tiny fix, biggest single win.**
- `transcription_provider` config key exists in `engagement.yaml:82` but is currently ignored by `tools/voice.py`. Faster-Whisper is one conditional branch away.
- `source` column exists on facts table but is unused; `attribution` is used instead. Mem0 single-pass behavior is already there at the call level.
- `drift_judge` already runs per-outbound and writes to `persona_drift_scores` — closing the loop is small.

## Recommended ship order (verification-informed)

The PARTIAL items are now the highest-leverage targets because infrastructure already exists. Re-prioritized:

**Ship this week (PARTIAL → DONE, mostly 1-line fixes):**
1. **Item 1** — Prompt caching: add `cache_control` to system block in `runtime.py`. Architecture already cache-aware.
2. **Item 4** — Faster-Whisper: wire the existing-but-unused `transcription_provider` config key in `tools/voice.py`.
3. **Item 18** — Reflexion drift correction: drift_judge already fires, just add verbal-correction call + table.
4. **Item 2** — Adaptive thinking: add `thinking`/`output_config` params in `runtime.py` (pure addition).
5. **Item 3** — Main-chat token instrumentation: extend the `background_tasks.cost_usd` pattern to main turns.

**Ship next (MISSING but small / well-scoped):**
6. Item 5 — Outbound voice notes (new tool + media_outbox enum extension)
7. Item 9 — Comfort/anger grammar in CLAUDE.md (doc edits + 2 mode flags)
8. Item 14 — Anniversary callbacks (`first_seen_date` column + annual cron)
9. Item 20 — Anti-binge hard stop (turn counter + threshold)
10. Item 10 — Daily Nothing (scheduler + runtime key)

---

## TIER 1 — Quick wins (verified)

### 1. Prompt caching on system prompt + core_blocks
**Why:** Cache reads on Sonnet 4.6 cost $0.30/M vs $3.00/M base. Hikari re-sends 5-15K tokens of unchanged system prompt every turn. Latency win documented at 11.5s → 2.4s. ([Anthropic prompt caching docs](https://platform.claude.com/docs/en/build-with-claude/prompt-caching))
**Fix:** `agents/runtime.py:575-591` — add `cache_control: {type: "ephemeral", ttl: "1h"}` to the system block in `ClaudeAgentOptions`. Pre-warm via `max_tokens=0` call in morning proactive cycle.
**Status: PARTIAL** — `runtime.py:439-442` has explicit comment about cache awareness ("never substituted here, since per-turn substitution would defeat the Anthropic prompt cache"). Cache reads are observed via `usage.cache_read_input_tokens` at `runtime.py:650-653`. But the actual `cache_control` parameter is **not set** on the system block. One-line addition, immediate 85-90% input-cost reduction.

### 2. Adaptive thinking + effort routing
**Why:** Sonnet 4.6 at `effort: medium` hits 79.6% SWE-bench vs Opus 4.6 high-effort 80.8%, ~60% lower cost. ([Anthropic adaptive thinking](https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking))
**Fix:** `agents/runtime.py:575-591` — add `thinking={"type":"adaptive"}, output_config={"effort":"medium"}` to the SDK call.
**Status: MISSING** — Zero references to `thinking`, `adaptive`, `effort`, or `budget_tokens` in the SDK call. Clean ship.

### 3. Agent SDK token spend instrumentation
**Why:** Anthropic split Agent SDK into separate $200/mo credit bucket on **June 15, 2026**. ([Agent SDK credit announcement](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan))
**Fix:** Extend the existing `background_tasks.cost_usd` pattern (`storage/db.py:157`, surfaced at `cockpit.py:479-495`) to the main chat path. Per-turn token counts already logged at `runtime.py:647-654` and `775-781` but never persisted as cost or aggregated.
**Status: PARTIAL** — Cost tracking works for dispatch jobs only. Token usage is logged for main turns but not converted to cost or persisted in a daily/monthly rollup table. Surface in `/cockpit stats` + add 80%-of-$200 alert.

### 4. Faster-Whisper local STT
**Why:** 4x faster than stock Whisper, identical accuracy, runs on Mac mini, kills the OpenAI key dependency. ([github.com/SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper))
**Fix:** `tools/voice.py` — wire the existing `transcription_provider` config key (currently ignored). Add `local_faster_whisper` branch in the provider switch. Keep OpenAI as fallback.
**Status: MISSING** — `config/engagement.yaml:82` already has `transcription_provider: openai_whisper_api`, but `tools/voice.py:29-76` ignores the value entirely and hardcodes OpenAI. The config key is aspirational. Single-conditional fix.

### 5. Outbound voice notes via ElevenLabs Flash v2.5
**Why:** Highest-impact presence upgrade per Kindroid power-user feedback. Flash v2.5: 75ms TTFB, $0.05/1K chars, ~$1/month at 20 notes/day. ([elevenlabs.io/pricing/api](https://elevenlabs.io/pricing/api))
**Fix:** 
- New `tools/voice_outbound.py` mirroring inbound-only `tools/voice.py`.
- Extend `storage/db.py:472` `media_outbox.kind` CHECK constraint from `('text','photo','sticker','document')` to include `'voice'`.
- Add `_send_outbox_voice` handler in `agents/telegram_bridge.py:394-399` alongside the other four.
- Mood-profile config in `engagement.yaml` (stability/similarity_boost per cycle phase).
- Agent calls the tool deliberately — never on a probability roll.
**Status: MISSING** — Zero ElevenLabs/TTS/sendVoice references. The media_outbox schema explicitly excludes voice. Clean ship.

### 6. Open PR aging proactive
**Why:** Solo dev workflow signal. GitHub subagent exists.
**Fix:** New `stale_pr_check` source in `agents/proactive.py`. Daily check for oldest PR open >72h with 0 reviews. Voice: *"your `refactor/session-store` PR has been open 4 days. nobody's looked at it."*
**Status: PARTIAL** — GitHub subagent exists at `agents/subagents/prompts/github.prompt.md:1-22` with `mcp__github__*` tool access. No `stale_pr_check` producer in `agents/proactive.py` or `agents/engagement/producers/`. The read capability is there; the daily-firing source isn't.

### 7. HuggingFace daily papers in morning brief
**Why:** Akhaliq-curated, higher signal than raw arxiv.
**Fix:** New `hf_papers` block in morning brief. One HTTP call to `huggingface.co/api/daily_papers`, free, filter by keyword match against `hikari_interests_pool.yaml`.
**Status: MISSING** — `tools/wiki/morning_brief.py:1-130` supports three topics (ai, noise, vibecode). `config/engagement.yaml:822-825` has arxiv config only. No HF references anywhere. Arxiv exists but is separate.

### 8. Calibration decile curve in Sunday ceremony
**Why:** Per-bucket accuracy drives recalibration 2-3x faster than scalar Brier (Tetlock). ([Fatebook calibration](https://fatebook.io/blog/concrete-benefits-of-making-predictions))
**Fix:** New `decision_calibration_curve()` in `storage/db.py`. GROUP BY 5 probability buckets, return actual outcome rate per bucket. `agents/decision_log.py` Sunday resolver injects the curve when n≥8.
**Status: PARTIAL** — `storage/db.py:4297` has `decision_brier_score(window_days=90)` returning `{n, brier, mean_predicted, mean_outcome}`. Sunday ceremony at `agents/scheduler.py:276-289` runs but only asks about individual decisions. Scalar exists, bucket math doesn't.

### 9. Comfort grammar + anger grammar specs in CLAUDE.md
**Why:** Two thinnest areas of existing voice spec.
**Fix:** New subsections in CLAUDE.md under `## response rules` with explicit triggers, rules, and example lines. Add `comfort_mode` / `anger_mode` flags in core_blocks or runtime_state. Detail in the original Tier 1 spec above.
**Status: PARTIAL** — Principle exists: `CLAUDE.md:22` ("if something actually matters, i drop the attitude"), `CLAUDE.md:37-38` (rule 8). `INTIMATE.md:39` mentions emotional half-life. No explicit `comfort_mode`/`anger_mode` subsections, no code flags. The directive is there; the grammar examples aren't.

### 10. The Daily Nothing
**Why:** Scarcity drives perceived value; notification-fatigue research consensus (2026).
**Fix:** Scheduler picks random weekday each Sunday → writes to `silent_day_this_week` runtime key. `agents/proactive_gate.py` reads it and suppresses proactive sources. User-anchored responses still work.
**Status: PARTIAL** — `tools/wiki/morning_brief.py:54` reads a `quiet_day` flag from briefing frontmatter (which is the CONTENT being quiet, not the day being silent). `config/engagement.yaml` has `quiet_start_hour`/`quiet_end_hour` (daily quiet hours) but no `weekly_silent_day` toggle. The scheduler picking a day and gating is missing.

---

## TIER 2 — Character depth + memory architecture

### 11. Three new character grammar moves
**Fix:** Three CLAUDE.md additions: Senjougahara inversion (stage 5+ rare precision disclosure, distinct from micro-leak), L2b Bartleby (zero-justification refusal slotted between L2 and L3), asymmetric concession (concede fact, hold stance, add depth).
**Status: PARTIAL** — Senjougahara is **named at CLAUDE.md:162** as a "precision callback" but the disclosure-grammar variant (no half-beat, flat statement, topic-switch) is undifferentiated. L2 Bartleby exists in the refusal table at `CLAUDE.md:142`; **L2b sub-variant doesn't exist**. Asymmetric concession is **principle-level at CLAUDE.md:26** ("concede the fact, keep the stance") and enforced by `config/engagement.yaml:276-285` sycophancy_guard, but no explicit template or named grammar move with the "add depth" twist. All three need grammar examples + (for L2b) a refusal-table row.

### 12. Cross-session emotional half-life
**Fix:** Next session opens 15-20% softer baseline (no reluctance opener, no barbs first 3-5 exchanges) when prior session used L3+ refusal, repair move, or overt warmth event.
**Status: PARTIAL** — In-session half-life is documented at `CLAUDE.md:83` and implemented at `config/engagement.yaml:456-469` with `decay_hours: 12`, `states: [quiet, raw, tired, sharp, soft]`, `heavy_moment_signals`. **Cross-session decay is missing entirely** — no `prior_session_heavy`, `session_entry_tone`, or `cross_session` sub-block. The runtime_state tracking for "last session ended heavy" isn't wired.

### 13. Slow-burn micro-tells
**Fix:** `slow_burn_tells[]` config array. Session-gated truths she names once (`"you're the only person i explain things to twice."`). Unlock thresholds at session 80/150/250. `i_keep_thinking` framing, max 1 per 40 turns.
**Status: MISSING** — `CLAUDE.md:163` has the principle ("wall develops holes, not a door — what accumulates is density"). `CLAUDE.md:187` has `i_keep_thinking` framing infrastructure. But no `slow_burn_tells[]` array, no session-count gates, no density-line unlock mechanism. Concept exists; ordered array and unlock infrastructure don't.

### 14. Anniversary callbacks
**Fix:** Add `first_seen_date` column to `lexicon`. New `significant_events` table (date, summary, type). Annual cron checks ±3 days. Surface sideways, stage 3+ only.
**Status: MISSING** — Lexicon table exists at `storage/db.py:190-203` with `phrase, weight, mention_count, origin_id, created_at` but **no `first_seen_date` column**. No `significant_events` table. No annual cron. Zero `anniversary` references in code. Clean ship with schema migration.

### 15. Mem0 multi-signal fusion + ACT-R decay
**Fix:** 
- Multi-signal: in `storage/retrieval.py`, extract query entities, +0.3 normalized bonus on entity-name match against `entities` table.
- ACT-R: migration adds `fact_category` to facts. Replace `compute_recency_score()` with category-specific τ (events 3d / preferences 21d / facts 29d) + ε=0.15 noise.
**Status: PARTIAL** — Park weights and hybrid semantic/BM25 (0.6/0.4) implemented at `storage/retrieval.py:9-136`. Entity-linking infrastructure exists (`fact_entities` table + `fact_entities_link` write path at `reflection.py:238`) but **entities are write-only — never consulted during retrieval scoring**. Ebbinghaus decay at `storage/retrieval.py:229-254` uses single `tau_base=604800s` (7 days) with `tau *= 1.5^hit_count`. **No category-specific τ, no noise term.** Both pieces missing.

### 16. Single-pass hierarchical fact extraction
**Fix:** Replace multi-call reflection with one DeepSeek call. New `source` column on facts (`user|hikari|inferred`). 0.7× weight on `hikari` source to prevent self-reinforcing loops.
**Status:** Two pieces, two statuses.
- **16a Single-pass extraction: EXISTS.** `agents/reflection.py:101-177` uses one unified YAML schema (new_facts, supersede, observations, noticings, entities, peer_update, thought, preoccupation) in a single LLM call. Already done.
- **16b Source column: PARTIAL.** `storage/db.py:78` has `source TEXT`. Reflection writes `attribution="hikari_inferred"` (`reflection.py:232, 268`), not `source`. `storage/retrieval.py:58-64` applies `_attribution_multiplier()` (user_stated 1.2× / user_corrected 1.1× / hikari_inferred 0.9×). The intended `source={user,hikari,inferred}` separation is not used; `attribution` is used instead. The 0.7× weight per spec isn't applied. Pick one column and standardize.

### 17. Background research subagent
**Fix:** Tag open_loops with `research_intent: true` on cue keywords. New `agents/subagents/research_worker.py` runs 10:00-12:00, max 2/day, uses existing research subagent. Writes `research_summary` back to the loop. `callback_surface.py` new `research_callback` source.
**Status: MISSING** — `agents/subagents/` has the on-demand `research` subagent for live web queries, but no scheduled background worker. No `research_intent` flag in tasks table. No `research_callback` source. No 10:00-12:00 cron. Existing research subagent is request-only.

### 18. Reflexion-style drift correction loop
**Fix:** On `drift` verdict from `agents/drift_judge.py`, DeepSeek generates one-sentence verbal correction. Store in new `voice_corrections` table (FIFO 10). Inject most recent 3 into next turn's context as `# voice-corrections` block.
**Status: PARTIAL** — `agents/drift_judge.py:100-173` already fires per-outbound (sampled), called from `telegram_bridge.py:584, 3061`, scores into `storage/db.py:235-244` `persona_drift_scores` table. **Loop is open** — verdicts are logged but never feed back into the next turn. No `voice_corrections` table, no correction-generation call, no injection. The infrastructure is 80% there; the closing loop is small.

### 19. Second-order peer model
**Fix:** Add `their_model_of_me: dict` to PeerRepresentation TypedDict. Quarterly DeepSeek update from messages where user makes meta-claims about Hikari. On-demand injection via recall.
**Status: MISSING** — `agents/peer_model.py:31-49` defines one-way `PeerRepresentation` (communication_style, values, domain_expertise, current_concerns, blindspots, summary). Zero `their_model`/`second_order`/`theory_of_mind` references. Clean ship — single field addition + quarterly extraction.

### 20. Anti-binge hard stop at 40 turns
**Fix:** Track `session_turn_count` in working_memory. At ≥40, set `session_closed`. Next message → Moshfegh unavailability line. New session = clean start.
**Status: MISSING** — `config/engagement.yaml:854-859` has `default_max_turns: 4` (per-turn cap, not session cap). No `session_turn_count`, `session_closed`, or `anti_binge_turn_limit` anywhere. Clean ship.

---

## TIER 3 — Larger plays

### 21. MCP Gateway with lazy tool loading
**Status: PARTIAL** — `agents/mcp_manager.py:1-15` already implements lazy bucket-3 server spawning with idle TTL shutdown ("spawn ON FIRST acquire and shut down after idle TTL"). This is per-server lazy pooling. The formal **gateway abstraction layer (mcp-agent wrapper exposing `gateway_list_servers`, `gateway_load_server`, etc.) is missing**. Token savings from lazy + caching are partial vs the full ~25K/turn from a true gateway that delays tool-descriptor hydration. Decide whether the existing implementation is enough — it might be.

### 22. FLUX.1 Kontext [pro] selfie engine + optional Hikari LoRA
**Why:** Better in-context character consistency than current provider.
**Fix:** Switch provider in `tools/photos/_shared.py`. Store canonical references in new `config/hikari_reference_images/`. Optional LoRA via fal.ai portrait trainer ($2-5 one-time).
**Status: PARTIAL** — **Current provider is FLUX.2-Klein via OpenRouter** (`tools/photos/_shared.py:37-38` — `black-forest-labs/flux.2-klein`), NOT NanoBanana as the initial mapping suggested. The system reads a text base prompt from `assets/APPEARANCE.md:5-10` and appends mood-gated scene suffixes. Zero reference-image infrastructure. To upgrade: swap to Kontext endpoint + add reference-image injection path. Decide if FLUX.2-Klein is good enough; the reference-image upgrade is the real win regardless of model.

### 23. Scene photos (no face required)
**Status: MISSING** — `tools/photos/` has `__init__.py, _shared.py, generate.py, classify.py` only. The mood-scene lookup table at `_shared.py:43-48` is for selfie variants. No environmental/non-selfie branching. New tool needed.

### 24. Oura readiness signal (opt-in)
**Status: MISSING** — Zero Oura/Whoop/HRV/wearable/biometric references. `.env.example` doesn't mention Oura. Clean ship.

### 25. Linear MCP + Readwise MCP
**Status: MISSING with caveat** — 
- Linear is aspirational in `.env.example:127` ("Linear MCP — auth flows via OAuth on first use") but NOT in `.mcp.json` (current 8 servers: apple_events, apple_shortcuts, duckdb, github, google_workspace, notion, playwright, youtube_transcript).
- **Readwise was deliberately removed 2026-05-21.** `agents/engagement/producers/readwise_daily_review.py:1-14` is explicitly stubbed: `"Do NOT re-add the Readwise MCP server"` until Readwise migrates to hosted HTTP. **Do not re-add.** Linear is the only side of this proposal worth pursuing.

### 26. Annual review ceremony (Dec 26-28)
**Status: MISSING** — No `annual_review.py`, no December cron in `scheduler.py`. `future_letter.py` is monthly first-Sunday only and separate. Clean ship.

### 27. Belief journal with 90-day resurface
**Status: MISSING** — `agents/belief_frame.py:1-152` handles past-tense beliefs only (`BELIEF_RE` at line 45: `"i think|i believe|..."`). No future-tense detector, no `belief_journal` table, no `resurface_at` column, no maturation logic. Clean ship.

### 28. Identity drift detector
**Status: MISSING** — No identity-claim regex in `belief_frame.py`, no `claim_type='identity'` flag, no cross-reference between identity claims and receipts/episodes. Clean ship.

### 29. Self-model block (observer == observed)
**Status: MISSING** — `agents/peer_model.py:31-49` is one-way (user-only). No `SelfRepresentation` TypedDict. No `# self-model` injection. No `current_voice_register`/`recent_deflection_rate`/`drift_vectors` tracking. The drift canary runs weekly in a separate agent and never feeds back into a self-model. Clean ship — extends peer_model.py with parallel TypedDict.

### 30. Sensor-based wake conditions
**Status: MISSING** — `apple_shortcuts` MCP is registered but no signal endpoints, no `activity_signals.py`, no signal table. `agents/proactive.py:386-450` does GPS-based location patterns but no activity-signal scoring. Clean ship — three Shortcut routes + endpoint + scoring boost in `proactive_gate.py`.

---

## SKIP / DEFER

- **Voice circles (Telegram round videos)** — uncanny without avatar system.
- **Ambient audio cues** — out of character, performative.
- **Computer use on Mac** — narrow daily value, heavy consent surface; opt-in only.
- **Custom Hikari fine-tune** — Claude constitutional + CLAUDE.md is doing the work. Revisit only if drift becomes measurable.
- **Friend.com / Limitless wearable** — Friend has mixed reviews, Limitless is acquired/dormant.
- **Readwise re-add** — see Item 25. Project explicitly forbids it until hosted HTTP MCP exists.
- **Duolingo-style streak counters** — would break voice. Non-guilt continuity marker is in-character substitute.
- **Multi-channel (Discord/iMessage)** — Telegram-pure is part of character.
