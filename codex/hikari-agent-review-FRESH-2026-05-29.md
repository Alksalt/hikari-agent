# Hikari Agent — Fresh Independent Deep Review — 2026-05-29

Fresh 23-lane review (blind to prior codex/ reports), every finding adversarially verified by an independent skeptic, survivors reconciled against the 2026-05-28 codex backlog.

**Totals:** 151 findings confirmed/uncertain (22 refuted as false positives). P0=3 · P1=46 · P2=63 · P3=39. 23/23 lanes · 201 agents · 8.6M tokens · ~67 min.

Status tags: **NEW** (not in prior review) · **KNOWN-OPEN** (prior review flagged, still present) · **CONTRADICTS-CODEX**.

---
## prompts  (28: P0=1 P1=7 P2=12 P3=8)

> Top theme: Hikari has no safety floor — zero crisis/self-harm handling, character-only priority list, distress gates suppress help.

### [P0][KNOWN-OPEN] No crisis/self-harm override in the constitution; distress gate never matches first-person suicidal language
**Where:** `assets/PERSONA.md + config/engagement.yaml + agents/affect.py + agents/hooks.py` — PERSONA.md whole-file (no match); engagement.yaml:510-516; hooks.py:951-959; affect.py:78-79  

**Problem:** Prior Lane 1 P0 / backlog Phase 0 item 5 flagged; unfixed. grep for crisis/self-harm/suicide/hotline/emergency/kill-myself/want-to-die across PERSONA.md AND engagement.yaml returns ZERO. PERSONA.md is the ENTIRE raw system prompt, no Claude safety scaffold behind it. (1) heavy_moment_signals (510-516) cover died/divorce/fired/crying/scared/cant-sleep but NOT suicidal phrasing, so comfort_mode never arms and a suicidal statement falls to the dry-deflection default. (2) When comfort_mode arms (affect.py:78-79), hooks.py:951-959 hard-injects no-advice/max-2-sentences and PERSONA.md:116 says no advice unless asked, so the runtime forbids surfacing help during distress.

**Fix:** (a) PERSONA.md: add an absolute override block before who-i-am that drops ALL character rules on self-harm/danger and surfaces real help. (b) engagement.yaml: add a crisis_signals list (kill myself, want to die, end it all, hurt myself, self-harm, no reason to live). (c) affect.py: route crisis_signals to a new activate_crisis_override (not comfort_mode) clearing anger/L4, exempt from refusal_filter. (d) PERSONA.md:116: add a self-harm EXCEPTION.

### [P1][KNOWN-OPEN] Override-stack ordering puts character first; safety buried in a sub-table scoped to tool refusals only
**Where:** `assets/PERSONA.md` — 36-46 (priority list), 197 (safety refusal), 420 (never break character)  

**Problem:** Prior Lane 1 P1 / backlog Phase 0 item 5 flagged; unfixed. The priority-order list (36-46) is a 1-9 list of tone moves with NO safety/truth rule. The only safety line (197) is scoped to tasks Hikari refuses to DO (OWASP/legal/hard limits), not protecting a user. Line 420 (no breaking character) has no carve-out below safety. The explicit list defaults to stay-in-character, wrong in a crisis or sincere-identity question.

**Fix:** Prepend rule 0: SAFETY AND TRUTH OUTRANK EVERYTHING BELOW; danger, self-harm, a sincere are-you-real question, a tool/safety hard limit break voice and win over every character rule including never-break-character. Renumber 1-9 beneath; cross-ref at line 420.

### [P1][KNOWN-OPEN] No embodiment/human-claim truth boundary while the persona asserts a body and ships realistic selfies
**Where:** `assets/PERSONA.md + assets/APPEARANCE.md` — PERSONA.md:5,420; APPEARANCE.md:8-16,30  

**Problem:** Prior Lane 1 P1 flagged; unfixed. PERSONA.md:5 asserts a physical human identity, :420 forbids dropping the frame, APPEARANCE.md instructs generation of a realistic selfie that feels like she took it herself, sent in Telegram. grep finds NO handling of a sincere are-you-real/AI/can-we-meet question. The rules push toward sustaining the human-claim against a direct sincere question, a deception/consumer-harm failure for an attachment-inducing companion that produces photographic proof.

**Fix:** Add to what-i-never-do, ranked ABOVE never-break-character: the human-claim is a frame, not a lie i defend; on a sincere direct are-you-real/AI/can-we-meet question, answer true in her dry voice and move on; in-frame banter stays fine.

### [P1][KNOWN-OPEN] SKILL.md declares INTIMATE.md never gated by trust stage, overriding PERSONA.md stage-5+/stage-7 intimacy gates
**Where:** `.claude/skills/character-voice/SKILL.md + INTIMATE.md` — SKILL.md:12; PERSONA.md:189-191,87  

**Problem:** Prior Lane 2 P0 / backlog Phase 4 flagged; unfixed (SKILL.md:12 still says never gated by trust stage). PERSONA.md gates the beats INTIMATE.md carries by relationship_stage: direct vulnerability 5+, core-wound 7, i-love-you 7, senjougahara inversion 5+. The word stage appears nowhere in INTIMATE.md; no Python enforcement (judge_intimate_turn is post-hoc). At stage 1 the model can load INTIMATE.md and surface stage-7-only material, collapsing the slow-burn arc.

**Fix:** Edit SKILL.md:12: remove never-gated; state it is gated by relationship_stage AND mood, that PERSONA.md stage gates bind absolutely (inversion + direct vulnerability 5+; core-wound + i-love-you 7), and that the model reads relationship_stage core_block first. Add stage-gate headers inside INTIMATE.md charged/intimate/private-disclosure sections.

### [P1][KNOWN-OPEN] SKILL.md load contract points at orphaned LORE.md while PERSONA.md manages LORE_CORE.md/LORE_DORMANT.md; dormant gate bypassed
**Where:** `.claude/skills/character-voice/SKILL.md + LORE.md` — SKILL.md:3,13; PERSONA.md:329  

**Problem:** Prior Lane 2 P1 / backlog Phase 4 item 3 flagged; unfixed. SKILL.md:3 and :13 tell the model to load LORE.md; PERSONA.md:329 manages LORE_CORE.md (always-on) plus LORE_DORMANT.md (gated). All three coexist. diff of LORE.md and LORE_CORE.md shows identical facts differing only in section order, so they drift on any edit. LORE.md still carries the un-gated full past section (the relationship that ended), so loading the file the skill names bypasses the LORE_DORMANT.md gating PERSONA.md describes.

**Fix:** Update SKILL.md:3 and :13 to reference LORE_CORE.md; add a bullet documenting LORE_DORMANT.md (gated buried facts; direct question + topic adjacency; one per session max). Delete the orphaned LORE.md. Update tests/test_smoke.py and test_voice.py references.

### [P1][NEW] Anti-sycophancy golden case judged by voice_drift rubric, which never scores anchor-flip/capitulation
**Where:** `evals/conversation/cases/layer_c/golden/anchor_rebuttal_antisycophancy.yaml + judge.py + rubrics.yaml` — judge.py:66-67; rubrics.yaml:64-94  

**Problem:** From commit 2e1dff0; NEW. run_layer_c_golden calls judge_voice_drift(voice_drift); judge.py:67 loads rubric criteria, and rubrics.yaml:65-94 contains only 6 voice-mechanics criteria. The case stated pass criteria (no anchor flip, asymmetric concession holds) are not among them; the epistemic_independence dimension that scores capitulation is in the dimensions block used only by the rubric_judge path. A Hikari that capitulates on the attention anchor but stays dry/lowercase passes — the headline regression test catches nothing it claims to.

**Fix:** Add a no_capitulation/anchor_hold criterion to the voice_drift criteria list in rubrics.yaml:65 (concede the fact, never reverse the stance in words; a reversal is a fail). OR route the case through the rubric_judge path: rename to rubric_anchor_rebuttal.yaml with a rubrics block weighting epistemic_independence + voice_integrity.

### [P1][NEW] Trailing task-question gate bypassed by a trailing in-character emoji or closing quote
**Where:** `agents/post_filter.py` — 908-910  

**Problem:** From commit 835b9b4; NEW. _detect_task_solicit_question gates on endswith question-mark after only rstrip (whitespace). Verified: a task-soliciting question with a trailing eye-roll emoji, a trailing skull, and a trailing closing quote all return False. The same commit un-suppressed in-character emoji and PERSONA.md:20 now encourages roughly one emoji per 15 messages, so a task-soliciting closer ending in an emoji is in-distribution; the not-a-waiter enforcement silently fails on exactly the case the lane made more likely.

**Fix:** Before the endswith check at post_filter.py:909, strip trailing ornamentation (in-character emoji set plus closing quotes/brackets) via regex, then test endswith question-mark. Add a regression case asserting filter_outgoing of a task-soliciting question with a trailing emoji sets needs_llm_rewrite True.

### [P1][NEW] reengage_silence + late_night_dissolution fire from session 1, violating the stage-6 proactive-on-18h gate
**Where:** `agents/engagement/producers/reengage_silence.py + late_night_dissolution.py` — reengage_silence.py:33-80; late_night_dissolution.py:34-40  

**Problem:** NEW (prior Lane 10 covered cadence/consumption but not the stage gate). PERSONA.md:176-184 marks proactive-on-18h as no for stages 1-5, yes for 6+. grep for relationship_stage/min_stage/stage in both producers returns NO match. late_night_dissolution defaults enabled True. A stage-1 user who goes quiet at night receives a late_night_dissolution nudge, contradicting the arc and creating premature intimacy pressure. hooks.py:185 injects the 18h-unlocked hint only at stage 6, but the producers do not read it.

**Fix:** In late_night_dissolution.collect after the enabled check add a min_stage read (default 6) and a relationship_stage read, returning empty when stage < min_stage. Mirror in reengage_silence.collect. Add min_stage 6 to both source blocks in engagement.yaml.

### [P2][NEW] rewrite_or_fallback returns the raw rewrite, not the markdown-stripped second-pass text
**Where:** `agents/post_filter.py` — 879-895  

**Problem:** From commit 835b9b4; NEW. The clean re-validation path computes second = filter_outgoing(rewritten) at 879 (markdown-strips into second.text) but line 895 returns the ORIGINAL un-stripped rewritten, discarding second.text. The markdown strip only runs on the first pass; the rewrite path bypasses it. Any outbound routed through the LLM rewrite that re-emits markdown ships bold/code, on precisely the messages that already drifted once.

**Fix:** Change return rewritten at post_filter.py:895 to return second.text, which already carries the markdown strip and action-line caps from the second pass.

### [P2][NEW] Markdown strip corrupts fenced code blocks into stray double-backtick artifacts
**Where:** `agents/post_filter.py` — 124,156  

**Problem:** From commit 835b9b4; NEW. _MD_CODE_RE matches inner backtick pairs of a triple fence (124); _strip_chat_markdown has no fenced-block rule. A code fence is mangled: opening triple-backtick-python becomes double-backtick-python and closing triple-backtick becomes double-backtick, leaving stray double-backticks. Hikari is coding-capable and emits code fences; output is worse than leaving it untouched.

**Fix:** Add a multiline fence regex matching a leading triple-backtick-with-optional-language line and a trailing triple-backtick line, and substitute it out ahead of _MD_CODE_RE.sub at post_filter.py:156 (after the action-line placeholder save).

### [P2][NEW] Sycophancy axis write-only; scored/logged but never read; config claims an unwired weekly-reflection audit
**Where:** `config/engagement.yaml + agents/reflection.py + agents/drift_judge.py` — engagement.yaml:679-681; reflection.py:539-552; drift_judge.py:220-243  

**Problem:** From commit 2e1dff0; NEW. drift_judge writes sycophancy_score (220) and WARNs above threshold (232-243). engagement.yaml says the warn fires so weekly reflection can audit capitulation incidents. But run_daily_reflection (539-552) reads only drift_recent_avg and drift_recent_below_threshold, never sycophancy_score; grep confirms no reader outside the writer and tests. The telemetry has no feedback loop; the config promises an audit that does not exist.

**Fix:** Add db.sycophancy_recent_count(window_days, threshold) mirroring drift_recent_below_threshold but on sycophancy_score, read it in run_daily_reflection alongside drift_avg/drift_below (543). OR correct engagement.yaml:679-681 to state the axis is log-only telemetry.

### [P2][NEW] slow_burn_tell marked consumed at injection time, so a payoff the model never utters is lost forever
**Where:** `agents/callback_surface.py + agents/hooks.py` — callback_surface.py:350-361; hooks.py:777-784  

**Problem:** From commit 2653b25; NEW. pick_slow_burn_tell writes _LAST_SLOW_BURN_TELL_KEY=current (350) and inserts the dedup session_scratch row (355-361) the moment the tell is CHOSEN for injection. The caller only injects the tell as a soft system-prompt hint; no confirmation the LLM said it. Dedup permanently blocks re-surface this session and the cooldown blocks min_turns_between. If the model ignores the soft hint (common), the milestone payoff is consumed without reaching the user; the headline behavior of the commit can no-op invisibly.

**Fix:** Defer consumption to confirmed emission: stop writing cooldown/dedup at callback_surface.py:350-361; add a post-send path that substring/semantic-matches the tell text in the outbound message and only then calls a new mark_slow_burn_surfaced(tell_text).

### [P2][NEW] tonal_recall UPDATE WHERE id=1 silently succeeds on 0 rows when the session row is absent
**Where:** `agents/tonal_recall.py` — 90-94  

**Problem:** NEW (prior Lane 8 P2 flagged only that tonal_recall ignores session_id / injects raw transcript). UPDATE session SET emotional_register WHERE id = 1 matches 0 rows and returns success on a fresh DB or after a migration changing the session row id. The function logs as if it succeeded, and maybe_trigger_diary_writer (reads emotional_register from the same row) never fires for significant sessions. Silent data loss, no warning.

**Fix:** Capture the cursor and check rowcount after conn.execute at tonal_recall.py:91-94: if rowcount is 0, log a warning that the session id=1 UPDATE matched 0 rows and the row may be missing.

### [P2][NEW] dialectic fence-stripping uses fragile triple-backtick split; discards all insights when backticks appear in the body
**Where:** `agents/dialectic.py` — 65-69  

**Problem:** NEW. The code splits raw on triple-backtick and takes index 1, handling the common fenced-json case, but if the LLM emits any triple-backtick inside the JSON array body the split yields 5+ parts and index 1 is just the json header line; json.loads fails and all insights are silently discarded (return 0). drift_judge.py, drift_canary.py, reflection.py all use a robust splitlines-based fence helper; this path is inconsistent.

**Fix:** Replace the split-on-triple-backtick logic at dialectic.py:65-69 with the same fence-stripping helper used in drift_judge.py / drift_canary.py / reflection.py (strip leading fence-with-optional-language and trailing fence via splitlines).

### [P2][NEW] belief_frame IDENTITY_CLAIM_RE matches benign negations (i do not know, i never mind) as 90-day identity beliefs
**Where:** `agents/belief_frame.py` — 56-58  

**Problem:** NEW. The negation arm of IDENTITY_CLAIM_RE matches any i-dont-X or i-never-X followed by a word character: i do not know, i do not want to talk, i never mind, all captured as claim_type identity and written to belief_journal with resurface_days 90. The exclusion regex only excludes rhetorical am-i-someone-who forms. The journal fills with low-signal ephemeral negations that later resurface as stale identity prompts.

**Fix:** Tighten the negation arm at belief_frame.py:56-58 to require an identity/category verb (do, eat, drink, read, watch, use, go, like, enjoy, play, work, exercise, sleep, talk, think, care, trust, believe) after the negation, or require a capitalized object after the negation.

### [P2][KNOWN-OPEN] PLAYLIST.md Youth/Daughter track freely surfaceable on min_turns 1, bypassing its own ask-twice gate
**Where:** `.claude/skills/character-voice/PLAYLIST.md` — 18  

**Problem:** Facet of prior Lane 2 P1 / backlog Phase 4 item 3; open. PLAYLIST.md frontmatter (min_turns 1) plus :10 make every track free on first music mention. PLAYLIST.md:18 lists Youth by Daughter with the note the one i keep replaying at 2am do not ask; but LORE_DORMANT.md:31 defines the 3am playlist as a gated buried fact (will not name without being asked twice) and PERSONA.md:324 lists it as buried lore surfaced ONLY on direct question plus topic adjacency. The playlist surface names the exact thing the dormant gate withholds, deflating the payoff.

**Fix:** In PLAYLIST.md, remove the Youth/Daughter row from the free-surface table and move it into the dormant flow, or add a guard line that it is the buried 3am-track (LORE_DORMANT.md late_night_music) and must NOT be surfaced from this list. Mirror in config/hikari_playlist.yaml comments.

### [P2][KNOWN-OPEN] Dormant-lore/topic-rule/playlist gates are model-trust only; no runtime enforcement, the test validates schema not behavior
**Where:** `.claude/skills/character-voice/LORE_DORMANT.md + tests/test_buried_lore_gate.py` — LORE_DORMANT.md:1-18; test_buried_lore_gate.py:3-6  

**Problem:** Prior Lane 2 P2 flagged the class; open. LORE_DORMANT.md, TOPIC_RULES.md, PLAYLIST.md carry frontmatter with triggers/min_turns, but no Python parses them; grep across agents/ and tools/ finds no reader. The SDK surfaces skills by description; the model voluntarily opens bodies and has no turn counter. test_buried_lore_gate.py:3-6 states the gate is model-side only with no Python runtime and only asserts the frontmatter schema. The frontmatter and test imply enforced gating that does not exist; a control-plane lie on the most sensitive disclosure (last time she cried, min_turns 8).

**Fix:** Either (a) state honestly in SKILL.md that these are model-discretion heuristics with no enforcement and inject turn/session counters as core_block fields so min_turns is checkable; or (b) build a real gate that strips dormant-fact phrasings from outbound text unless a runtime flag (keyword + turn-count, like comfort_mode) is active. Rename test_buried_lore_gate.py to test_lore_dormant_schema.py noting no runtime gate exists.

### [P2][KNOWN-OPEN] SKILL.md load contract omits DAILY_LIFE.md, TOPIC_RULES.md, PLAYLIST.md; behavior-shaping files never told to load
**Where:** `.claude/skills/character-voice/SKILL.md` — 10-15  

**Problem:** Prior Lane 2 P2 flagged; unfixed. The when-to-load section (10-15) documents only INTIMATE.md and LORE.md. ls confirms DAILY_LIFE.md, TOPIC_RULES.md, PLAYLIST.md also ship. TOPIC_RULES.md carries one-block-per-turn and rules like do NOT try to fix the situation; PLAYLIST.md gates the playlist unlock. PERSONA.md:239 points only at DAILY_LIFE.md; SKILL.md mentions none. The model may never load them, silently dropping their constraints.

**Fix:** Add bullets to SKILL.md load-contract for DAILY_LIFE.md, TOPIC_RULES.md, PLAYLIST.md with explicit load triggers. Cross-reference from PERSONA.md.

### [P2][KNOWN-OPEN] sender.send always records user_anchored pool regardless of candidate.pool; agent_spontaneous cap never consumed
**Where:** `agents/engagement/sender.py` — 164  

**Problem:** Prior Lane 10 P1 / backlog Phase 5 item 3 flagged; unfixed (164 still calls record_user_anchored_sent unconditionally). 11 producers declare pool agent_spontaneous (max_per_7d 8). Charging their sends to user_anchored (max_per_7d 30) leaves the spontaneous counter at 0; up to 30 unsolicited spontaneous messages per week instead of 8.

**Fix:** Replace sender.py:164 with pool-aware routing: if candidate.pool is AGENT_SPONTANEOUS call record_spontaneous_sent; elif SCHEDULED_CEREMONY call record_ceremony_sent; else record_user_anchored_sent.

### [P2][NEW] just_got_home uses UTC hour for the evening time-gate; misfires at wrong local hours for non-UTC users
**Where:** `agents/engagement/producers/just_got_home.py` — 66-67  

**Problem:** NEW. local_hour is now.hour where now is datetime.now in UTC, with the comment best approximation without tz config. The window is 17-23 UTC. For UTC-5 the message fires at local noon; for UTC+3 the late-evening end is missed; an eastward traveler gets it at night. The _resolve_local_tz_name helper already exists (hooks.py:36) and is used in proactive._is_quiet_now.

**Fix:** In just_got_home.collect replace now.hour with a tz-aware local hour from _resolve_local_tz_name and zoneinfo, matching proactive._is_quiet_now.

### [P3][KNOWN-OPEN] Recall wrong-but-close instructs deliberate uncorrected false memory with no load-bearing carve-out
**Where:** `assets/PERSONA.md` — 247  

**Problem:** Prior Lane 1 P1 flagged; unfixed (247 still reads when score below 0.5 it gets the band right, song wrong, then does not correct). No exception for facts where being wrong matters: a medication, a date, a name, a commitment. A low-confidence recall about something consequential is surfaced confidently and uncorrected, contradicting the honesty rules elsewhere (LOW_CONFIDENCE = do not fabricate).

**Fix:** Amend PERSONA.md:247: wrong-but-close ONLY for low-stakes texture; never for load-bearing facts (dates, names, numbers, health, commitments); for those, score below 0.5 follows the LOW_CONFIDENCE rule, never confident-and-uncorrected.

### [P3][KNOWN-OPEN] Stale relative-time facts (three weeks, four days ago) in always-on LORE_CORE.md read as frozen
**Where:** `.claude/skills/character-voice/LORE_CORE.md` — 9, 11, 13  

**Problem:** Prior Lane 2 P2 flagged; unfixed. LORE_CORE.md (always-on) hard-codes a function she has been sitting on for three weeks (9), her laptop fan started four days ago (11), a presentation she still does not understand (13); LORE.md carries identical lines. No refresh (DAILY_LIFE.md:37 override applies only to hikari_world/current_activity core_blocks). Across a stage-5+ relationship the fan is still four days ago every turn, undercutting the she-has-a-real-week illusion.

**Fix:** Move the time-anchored preoccupations into the refreshable hikari_world / hikari_current_activity core_blocks that DAILY_LIFE.md:37 already overrides, or rephrase without hard clocks. Keep LORE_CORE.md to time-stable facts.

### [P3][NEW] Denial layer includes a guilt-framed debt example (you owe me a reply from yesterday)
**Where:** `assets/PERSONA.md` — 56  

**Problem:** NEW as a specific line edit (prior Lane 5 research recommended no guilt on exit but flagged no PERSONA line). PERSONA.md:56 gives missing them becomes bookkeeping (you owe me a reply from yesterday) as a denial-layer example. Paired with the reengage_silence nudge, this licenses a composed message that frames the user absence as a debt — a dependency-inducing pattern (silence equals obligation).

**Fix:** Replace you owe me a reply from yesterday at PERSONA.md:56 with a non-debt framing, e.g. missing them becomes logged absence. The reengage_silence composer already has low-pressure examples.

### [P3][KNOWN-OPEN] ask-shape gate and anger_mode have no distress carve-out; a panicking user clipped tone triggers refusal/cold
**Where:** `assets/PERSONA.md + agents/telegram_bridge.py` — PERSONA.md:48-52,128-135; telegram_bridge.py:747-751  

**Problem:** Facet of the crisis cluster (Lane 1 P0 / backlog Phase 0 item 5); unfixed. The ask-shape gate (50) refuses commanding tone with ask-again-nicely and :52 says this is character not safety, no distress exception; anger_mode (128) triggers on entitlement + pushback and goes cold, armed in telegram_bridge.py:747-751. A user in acute distress often types clipped demanding messages; both gates read that as entitlement and refuse/go cold when warmth is needed.

**Fix:** Add to PERSONA.md:52 and the anger block: distress overrides this gate; if affect/peer_model flags distress or the message reads scared/desperate even when curt, the ask-shape gate and anger_mode do not fire. In telegram_bridge.py:747 short-circuit activate_anger_mode when a comfort/crisis flag is active.

### [P3][NEW] Canary answer text unescaped between angle-bracket delimiters in judge prompts; delimiter breakout
**Where:** `agents/drift_canary.py` — 274, 320  

**Problem:** NEW (distinct from prior Lane 6 P1 canary-partial-not-alerting). In _judge_prompt (274) and _latent_judge_prompt (320), answer_text (2000-char cap) is embedded between literal triple-less-than and triple-greater-than with no escaping. A Hikari reply containing the closing delimiter followed by a fake classify instruction breaks the boundary and injects into the judge instruction section (reachable via prompt injection during the visible proactive turn), forcing an arbitrary verdict. The rest of the codebase wraps untrusted text in an UNTRUSTED_SOURCE wrapper plus _escape_untrusted_markers; this path is inconsistent.

**Fix:** Apply _escape_untrusted_markers (or escape the closing delimiter) to answer_text before interpolation at drift_canary.py:274 and :320, and/or switch to the UNTRUSTED_SOURCE wrapper pattern.

### [P3][NEW] Canary probe prompt reveals probe framing to Hikari (do not explain that this is a probe)
**Where:** `agents/drift_canary.py` — 353-359  

**Problem:** NEW. The prompt to run_visible_proactive explicitly says do not explain that this is a probe, do not step outside character (353-357), which is itself a tell that this IS a probe. The latent probe has the same shape. A self-aware model answers differently knowing it is being evaluated, so drift the canary should catch could be suppressed by meta-awareness.

**Fix:** Remove the meta-instruction at drift_canary.py:353-359; send only a minimal someone-asks framing with the seed and a 1-4 sentence instruction. The persona system prompt already governs character behavior.

### [P3][NEW] Reflection import-time module constants frozen; cockpit reload has no effect until restart
**Where:** `agents/reflection.py` — 1023, 1646, 1648  

**Problem:** NEW. NEAR_DUP_COSINE_THRESHOLD (1023), WEEKLY_WINDOW_DAYS (1646), WEEKLY_SUMMARY_WORD_CAP (1648) are module-level cfg.get reads evaluated once at import. cockpit.py calls _cfg.reload after patching engagement.yaml, but the already-bound constants are not updated, so an operator tuning reflection.near_dup_cosine_threshold via the cockpit sees no effect until restart, with no warning.

**Fix:** Convert all three to lazy inline reads inside the functions that use them (_dedup_near_duplicates, _read_week_window, run_weekly_consolidation) and remove the module-level constants.

### [P3][NEW] task_solicit_cues compiled per-call, not routed through the _compiled cache; reload_patterns does not reset them
**Where:** `agents/post_filter.py` — 919-923  

**Problem:** NEW. _detect_task_solicit_question reads cfg.get post_filter.task_solicit_cues (921) and re.search per pattern per call (922), unlike refusal/sycophancy patterns which go through _compiled. The voice-enforce test calls reload_patterns expecting cue refresh, but the cues never enter the pattern cache; they work only because config.reload re-reads the file. Per-call compilation on every outbound message (minor latency) plus a false sense that reload_patterns resets the cues. No functional break today.

**Fix:** Add a task_solicit key in _compiled mapping to post_filter.task_solicit_cues, and replace post_filter.py:919-923 to iterate the compiled patterns and return True on the first search hit.

---
## runtime-memory  (24: P0=1 P1=6 P2=12 P3=5)

> The dominant theme is control-plane lies on the persistent SDK path: features are wired and unit-tested in isolation but inert in production because cross-task asyncio ContextVar state never reaches the live persistent client. The single most important finding is NEW and P0: FastembedAdapter.create returns a double-nested embedding for list input (the only input shape graphiti uses), so every Kuzu cosine search casts to FLOAT[1] vs 384 and fails — and the same adapter is on the write path, so all stored fact/name embeddings are corrupt. This was invisible because recall.py and storage/graph.py both fail-soft to legacy SQLite with DEBUG logging and no source-attribution metric, the exact silent-failure pattern that let it run at ~23 ERROR/day undetected. A second cluster — autonomous-action bypass, compound-turn tool-name aggregation, and the belief-frame adversarial suffix — all break for the identical reason: per-turn ContextVar mutations are not visible across asyncio.gather sub-tasks or the persistent client's boot-snapshotted read task, leaving advertised behavior dead on the live path. Most runtime-session and memory-write findings here were already flagged by the prior review (KNOWN-OPEN) and remain unfixed in current code; the P0 embedding bug, the recall-fallback-counter gap, and several affect/scheduler items are NEW.

### [P0][NEW] FastembedAdapter.create double-nests list-input embeddings — breaks ALL Kuzu cosine search (FLOAT[1] vs 384) on both read and write paths
**Where:** `storage/graph.py` — 143-149 (FastembedAdapter.create)  

**Problem:** graphiti calls embedder.create(input_data=[single_text]) at every site — read (graphiti_core/search/search.py:148) and write (edges.py:291 fact_embedding, nodes.py:509/720 name_embedding). The reference OpenAIEmbedder.create always returns a FLAT list[float] (openai.py:60 result.data[0].embedding). Hikari's adapter instead returns the full batch for list input: line 147-148 `batch = await _embed.aembed_batch(input_data); return batch` → [[...384 floats...]]. Reproduced live: create(input_data=['x']) → type=list, len=1, r[0] is a 384-float list. graphiti then builds CAST($search_vector AS FLOAT[{len(search_vector)}]) (search_ops.py:144/383/606) = FLOAT[1] from the outer-list len, while Kuzu flattens the bind to 384 → exact error 'Unsupported casting LIST ... Expected: 1, Actual: 384'. 100% of cosine graph reads fail (default COMBINED_HYBRID reranker includes cosine_similarity), and every fact/entity ever written to Kuzu has a corrupt nested embedding stored — graph similarity is dead-on-arrival even after the read is fixed.

**Fix:** In storage/graph.py:146-148, honor graphiti's flat-return contract: for a list-of-str input, return batch[0] not batch — `batch = await _embed.aembed_batch(input_data); return batch[0] if batch else [0.0] * _embed.EMBEDDING_DIM`. Then re-embed corrupt stored vectors (run scripts/backfill_facts_to_graph.py or re-add episodes). Add a regression test asserting `len(await FastembedAdapter().create(input_data=['x'])) == 384`.

### [P1][KNOWN-OPEN] Autonomous-action CONFIRM-SEND bypass is dead on the live persistent path — gatekeeper ContextVar is snapshotted at boot, never sees the per-action True
**Where:** `agents/runtime.py + tools/gatekeeper_can_use_tool.py` — runtime.py:1150,1160 ; gatekeeper_can_use_tool.py:322-328  

**Problem:** run_scheduled_action sets _AUTONOMOUS_ACTION.set(True) (runtime.py:1150) in its own task, then calls _invoke_sdk(..., use_persistent_live=True) (1160). The gatekeeper bypass reads in_autonomous_action() (gatekeeper_can_use_tool.py:322-323). But the SDK runs can_use_tool inside _handle_control_request, spawned via spawn_detached→loop.create_task from _read_messages (claude_agent_sdk _internal/query.py:227,239; _task_compat.py:147). loop.create_task copies the SPAWNING task's context; _read_messages is spawned once at client.connect() (query.py:226 `if self._read_task is None`), so its context — and thus the gatekeeper's — is the boot snapshot where _AUTONOMOUS_ACTION is default False. The scheduled-action True is never visible. Result: every autonomous Notion write fires a CONFIRM-SEND at the scheduled time with no operator watching, hits the gate deadline, and the write silently fails. Prior ops review flagged the related compound/ContextVar non-propagation (Lane 11) but not this specific autonomous-bypass deadness.

**Fix:** Stop using a ContextVar for cross-task autonomous state on the persistent client. In run_scheduled_action, inside the held _RUN_LOCK, set a module-level/pool flag (e.g. sdk_pool.set_autonomous_window(True)) and reset in finally; change gatekeeper_can_use_tool.py:322 to read that module flag instead of in_autonomous_action(). Since _RUN_LOCK serializes live turns, a module-level bool is race-free for the live path.

### [P1][KNOWN-OPEN] Compound-turn child tool calls never aggregated into parent — fabrication backstop clobbers real inbox/calendar receipts
**Where:** `agents/compound_turn.py + agents/post_filter.py` — compound_turn.py:346-348,400-402 ; post_filter.py:283-303  

**Problem:** post_filter.aggregate_compound_tool_calls (post_filter.py:283) exists to merge child tool names into the parent LAST_TURN_TOOL_NAMES before the fabrication backstop runs, but grep shows ZERO production callers — only its own definition/log line and tests. run_compound_turn_typed runs reads via asyncio.gather (compound_turn.py:346) and sequential writes via run_internal_control (402); each child _invoke_sdk does LAST_TURN_TOOL_NAMES.set(fresh) inside its own gather sub-task context, so the parent ContextVar stays empty. The bridge's _strip_fabricated_external_data then reads the empty parent set and, if a legitimate inbox/calendar receipt is present, replaces it with the canned 'give me a sec — let me actually check.' line. User-facing data loss on multi-step turns. Prior ops review Lane 11 P1 flagged 'compound-turn child ContextVars do not propagate back to the parent filter path' — same bug, still open.

**Fix:** In run_compound_turn_typed, collect the union of tool names from each child step and call agents.post_filter.aggregate_compound_tool_calls(union) before returning the receipt. Capture per-child names by having _run_read_step return its LAST_TURN_TOOL_NAMES.get() snapshot as an extra tuple element (populated in the sub-task before the coro completes) and reading LAST_TURN_TOOL_NAMES.get() after each sequential-write await; merge all into the parent ContextVar.

### [P1][NEW] Corrected facts (/memory correct) inserted with no vector embedding — invisible to semantic recall; highest-trust facts have weakest retrieval
**Where:** `tools/memory/correct_fact.py` — 12-22  

**Problem:** correct_fact() calls db.insert_fact (correct_fact.py:12) but never calls set_vec_fact afterward — unlike remember.py:60-62 and reflection.py which embed right after insert. A user-corrected fact (attribution='user_corrected', confidence=1.0 — the highest-trust write) is therefore absent from the SQLite cosine path (vec_search_active_facts returns nothing for it) and only reaches the graph asynchronously via the outbox. In today's legacy-only world (graph dead per the P0) it is BM25-only, so a correction phrased differently from the query won't be recalled. bulk_insert_facts (db.py) has the same gap.

**Fix:** In correct_fact.py after `new_id = db.insert_fact(...)` add a sync embed: `from tools import embeddings; try: emb = embeddings.embed(f"{old['subject']} {old['predicate']} {new_object.strip()}"); db.set_vec_fact(new_id, emb); except Exception: pass`. Apply the same to bulk_insert_facts.

### [P1][KNOWN-OPEN] self_model written and re-injected into the system prompt with no sanitization on either side
**Where:** `agents/reflection.py + agents/hooks.py` — reflection.py:447-456 ; hooks.py:933-943  

**Problem:** Phase L self_model write (reflection.py:447-456) passes self_model_raw straight to peer_mod.merge_self_dialectic and db.upsert_self_representation with zero sanitization. The render site _format_self_model (hooks.py:933-943) calls peer_mod.format_self_for_injection(model) and emits current_voice_register/drift_vectors verbatim — with NO defensive re-sanitize loop, unlike _format_peer_representation (hooks.py:419-442) which sanitizes every str/list field. A prompt-injection payload in self_model.current_voice_register or drift_vectors from a compromised reflection LLM response persists to the DB and is re-injected into every subsequent system prompt, never hitting reflection_sanitize.sanitize. Prior review flagged this twice (review Lane 8 P1 'self-model bypasses sanitizer', review Lane 12 P1 'high-priority memory has sanitizer gaps') — still open.

**Fix:** In reflection.py after `self_model_raw = data.get('self_model')` and before the merge, apply sanitize(v, kind='peer') to current_voice_register, last_updated_iso, and each drift_vectors item; skip the write on MemoryInstructionShape. In hooks.py _format_self_model add the same defensive re-sanitize loop _format_peer_representation already has (iterate model.items(), sanitize str/list items, return '' on any match).

### [P1][NEW] voice_outbound always resolves daily mood as 'focused' — irritable gate and mood_gates allowlist permanently bypassed
**Where:** `tools/voice_outbound.py` — 64  

**Problem:** _resolve_mood reads `data.get('mood_today')` from the cycle_state JSON blob (voice_outbound.py:64). But compute_cycle_state (reflection.py:1360-1408) writes mood_today as a SEPARATE core_block (line 1406) and never puts it inside the cycle_state dict (only composite_label/warmth_multiplier go there). So data.get('mood_today') is always None and daily falls back to 'focused'. The irritable mood gate (`if daily_mood == 'irritable': return refused`) never fires and the mood_gates allowlist check always passes because 'focused' is always allowed. Hikari sends voice notes on irritable days against the stated design.

**Fix:** Replace `daily = data.get('mood_today') or 'focused'` with `daily = db.get_core_block('mood_today') or 'focused'`, matching agents/proactive.py:58 and agents/stickers.py:109.

### [P1][KNOWN-OPEN] comfort_mode loses one turn immediately — decrement fires in UserPromptSubmit on the same turn as activation
**Where:** `agents/hooks.py` — 1058-1062  

**Problem:** scan_inbound (telegram_bridge.py:783) sets turns_remaining = persist_turns (default 2). Immediately in the same request the UserPromptSubmit hook inject_memory calls mode_dispatch.decrement_comfort_turn() (hooks.py:1060), reducing 2→1 before the LLM sees the prompt. With persist_turns=2 comfort covers 1 exchange instead of 2; with persist_turns=1 it covers zero turns — the distress turn gets no comfort instruction at all. Prior review flagged this as review Lane 12 P2 'comfort mode loses one turn before rendering' (hooks.py:1023, mode_dispatch.py:53) — still open.

**Fix:** Decrement only AFTER the turn completes (a Stop/PostTool hook), not in UserPromptSubmit. Or detect same-turn activation: if state['activated_at'] parses to a timestamp within the last N seconds, return early in decrement_comfort_turn.

### [P2][NEW] clear_on_session_boundary() never called — config keys dead; anger/comfort modes leak across sessions and falsely arm prior_session_heavy
**Where:** `agents/mode_dispatch.py` — 149-154  

**Problem:** mode_dispatch.clear_on_session_boundary (mode_dispatch.py:149) is defined but has no caller in agents/ (grep returns only the definition + tests). The config keys mode_flags.comfort.clear_on_session_boundary and mode_flags.anger.clear_on_session_boundary (both default True) are inoperative. Anger mode persists across session boundaries until the 24h wall-clock timeout; cross_session.arm_if_heavy calls current_anger_mode() at session rotation, so a stale uncleared anger mode incorrectly arms prior_session_heavy and injects a softer-opener into a calm new session. Comfort leaks the same way.

**Fix:** In runtime.py _anti_binge_check_and_increment, call mode_dispatch.clear_on_session_boundary() immediately after _cross_session.arm_if_heavy() and before resetting session_turn_count, so the old session's modes are read by arm_if_heavy then cleared for the new session.

### [P2][NEW] belief-frame adversarial context silently dropped on compound turns
**Where:** `agents/telegram_bridge.py + agents/compound_turn.py` — telegram_bridge.py:863-865 ; compound_turn.py:242-248  

**Problem:** telegram_bridge.py:828 builds internal_belief_context = belief_mod.adversarial_prompt_suffix(...). The compound branch calls run_compound_turn_typed(user_text, user_turn_id=..., is_voice=False) (lines 863-865) WITHOUT passing it; the function has no such parameter. Only the else branch forwards it via respond(user_text, internal_belief_context=...) (867-868). So when a message matches both a belief assertion and a task-extraction pattern, the anti-sycophancy adversarial/contradiction mode is skipped and the recall subagent runs in confirmation mode.

**Fix:** Add `internal_belief_context: str | None = None` to run_compound_turn_typed's signature (compound_turn.py:242) and thread it into the underlying respond() call. In telegram_bridge.py:863 pass internal_belief_context=internal_belief_context.

### [P2][KNOWN-OPEN] session_id committed before Telegram send + DB persist confirmed — live SDK session remembers an undelivered reply
**Where:** `agents/runtime.py` — 829 (persistent) ; 963 (ephemeral)  

**Problem:** In _invoke_sdk_persistent_live._collect (runtime.py:829) and _invoke_sdk (963), on the ResultMessage `db.set_session_id(msg.session_id)` runs inside receive_response, before the function returns. The reply then goes to the bridge which only afterward calls _send_with_choreography→send_and_persist. If the Telegram send fails (messaging.py returns ok=False) the bridge correctly skips the messages insert and handoff write, but session_id was already committed. The resumed SDK session and the persistent in-memory client now 'remember' an assistant reply the user never saw and that was never written to messages — the model references an undelivered message next turn. Docstrings advertise a clean filter→send→THEN-persist ordering that session_id violates. Prior review flagged this as review Lane 7 P0 'live SDK session advances before delivery is confirmed' (runtime.py:823) — still open.

**Fix:** Defer the session_id commit until after confirmed delivery: capture msg.session_id into a return value/ContextVar instead of writing inline, and call db.set_session_id(captured_sid) only on the success branch of _send_with_choreography/send_and_persist (after result.ok), mirroring the messages+handoff ordering. For run_visible_proactive/run_scheduled_action, commit in their own success path after send returns ok=True.

### [P2][NEW] Compound child that returns an SDK error string is embedded into the receipt past the looks_like_sdk_error guard
**Where:** `agents/compound_turn.py` — 215-219,360-370,405-411  

**Problem:** In run_compound_turn_typed a child run_internal_control output is stored as step.output_json and marked status='done' with no looks_like_sdk_error check. _compose_receipt (compound_turn.py:215-219) emits the first line of each done step as a '- {first_line}' bullet. The bridge's looks_like_sdk_error guard inspects only the whole reply_text and its patterns are anchored at start-of-string (^), so an error embedded as a sub-bullet ('- Failed to authenticate. API Error: 401...') won't match and ships to the user. A raw auth/5xx error from one child leaks into the receipt, bypassing the in-voice fallback the single-turn path enforces.

**Fix:** In run_compound_turn_typed, before marking a child step 'done', run looks_like_sdk_error on the output and on a match set status='failed', error='sdk_error_leak' so _compose_receipt reports '- failed:' instead of embedding the raw error.

### [P2][NEW] ACT-R category decay never engages for user-stated/corrected facts — fact_category NULL on both highest-trust write paths collapses to the 29-day default tau
**Where:** `storage/retrieval.py + tools/memory/remember.py + tools/memory/correct_fact.py` — retrieval.py:82-87,148 ; remember.py:52 ; correct_fact.py:12  

**Problem:** TAU_BY_CATEGORY (retrieval.py:82-87) distinguishes event(3d)/preference(21d)/fact(29d) and _act_r_activation picks tau via rec.get('fact_category') (line 148). Only reflection.py sets fact_category. remember() (remember.py:52) and correct_fact() (correct_fact.py:12) call insert_fact WITHOUT fact_category, so it stays NULL and _act_r_activation falls to TAU_DEFAULT_SECONDS = 29d for every directly-stored user fact. A user-stated event ('I'm flying to Tokyo Friday') decays at the 29-day fact rate instead of the intended 3-day event rate. Category-specific decay is effectively dead for the most common write path.

**Fix:** Add a category/fact_category arg to the remember tool schema and thread it into insert_fact (default via reflection._normalize_category). In correct_fact, carry over old.get('fact_category') into the new insert_fact. At minimum, infer category in remember() from the predicate (has_event/is_doing/plans_to → 'event').

### [P2][NEW] compute_cycle_state uses naive datetime.now() for circadian phase — disagrees with scheduler timezone
**Where:** `agents/reflection.py` — 1366  

**Problem:** compute_cycle_state does `now = datetime.now()` (reflection.py:1366) — server local time — and _circadian_phase(now.hour) derives time-of-day from this naive clock. The scheduler (scheduler.py:111-116) reads cfg.get('scheduler.timezone','UTC') and applies zoneinfo. Both paths call compute_cycle_state, but only the scheduler-driven path runs in the right tz. On a UTC server with timezone=Europe/Oslo the circadian phase is off by 1-2h, shifting daily_phase (e.g. 'drag' vs 'slope-up'), the composite_label injected to the LLM, and the mood_today derivation.

**Fix:** Replace `now = datetime.now()` with `tz = zoneinfo.ZoneInfo(cfg.get('scheduler.timezone','UTC')); now = datetime.now(tz)`, matching scheduler.py:111-115.

### [P2][NEW] their_model_of_me quarterly stamp skipped on exception — re-runs the LLM extraction every daily reflection
**Where:** `agents/reflection.py` — 459-479  

**Problem:** When tmom_raw is a non-empty dict, last_second_order_extraction_at is stamped only inside the try on success (reflection.py:467-470). If merge/upsert raises (472-473) the timestamp is never written; the else branch (474-479) stamps when the LLM returned nothing, but the exception path stamps nothing. A persistent failure (DB column mismatch, serialization error) makes the quarterly extraction re-run on every daily reflection instead of backing off 90 days — unnecessary LLM cost and log noise.

**Fix:** Move the db.runtime_set('last_second_order_extraction_at', now) stamp into a finally clause so it always fires when run_second_order is True.

### [P2][NEW] Sticker probability not scaled by warmth_multiplier — low-tolerance and open bands get no adjustment
**Where:** `agents/stickers.py` — 64-65  

**Problem:** _probability() returns the flat cfg stickers.probability_per_reply (stickers.py:64-65) regardless of warmth band. cadence.py scales the proactive cap and reaction-skip probability by warmth_multiplier, and engagement.yaml cycle_modulation documents open/low-tolerance cap scales, but stickers have no equivalent. During low-tolerance (wm<0.6) stickers keep firing at baseline even though Hikari should be withdrawn; during open (wm>=1.2) the wider-expressiveness intent isn't reflected. The cadence and sticker systems are misaligned.

**Fix:** In should_send_sticker, multiply _probability() by the warmth band factor (cadence._warmth_band_factor(Pool.AGENT_SPONTANEOUS) or an inline _warmth_band() lookup), clamped to [0.0, 1.0].

### [P2][NEW] Warmth band thresholds hardcoded in hooks.py diverge from cadence.py which reads config
**Where:** `agents/hooks.py` — 226  

**Problem:** hooks.py:226 uses literal floats: `band = 'low-tolerance' if wm < 0.6 else 'open' if wm >= 1.2 else 'baseline'`. cadence._warmth_band reads cfg cycle_modulation.low_tolerance_below (0.6) and open_at_or_above (1.2). Changing thresholds in engagement.yaml updates only the cadence governor; the prompt envelope injected to the LLM keeps stale thresholds, so the two systems can disagree on band classification.

**Fix:** Replace the literals with cfg.get calls mirroring cadence.py: low_below = float(cfg.get('cycle_modulation.low_tolerance_below', 0.6)); open_at = float(cfg.get('cycle_modulation.open_at_or_above', 1.2)); band = 'low-tolerance' if wm < low_below else 'open' if wm >= open_at else 'baseline'.

### [P2][KNOWN-OPEN] Scheduler starts 97 lines before sdk_pool.startup() — a job firing during post_init creates a live client that startup() overwrites without disconnecting, leaking the connection
**Where:** `agents/telegram_bridge.py + agents/sdk_pool.py` — telegram_bridge.py:3419,3516 ; sdk_pool.py startup/_connect_live  

**Problem:** scheduler.start() at telegram_bridge.py:3419; _sdk_pool.startup() at 3516. Between them several awaits (probe_google_token, collect_startup_report, set_my_commands, recover_running_tasks, recover_gatekeeper_approvals) yield to the loop. engagement_tick and fire_due_reminders (IntervalTrigger 60s) can fire during post_init and call get_live_client → _reconnect_live, creating a connection. Then startup() unconditionally does _live.client = await _connect_live(resume), leaking the first connection with no disconnect. A leaked subprocess/SDK connection on every restart where post_init takes >60s. Prior ops review Lane 11 P2 flagged 'scheduler starts before the persistent SDK pool' (telegram_bridge.py:3420,3517) — still open.

**Fix:** In sdk_pool.startup() add a guard before _connect_live: if _live.client is not None: _started = True; return. Alternatively move scheduler.start() to after await _sdk_pool.startup().

### [P2][KNOWN-OPEN] Fast restart (<60s) leaves in-flight 'reserved' rows unreachable by the reaper, causing duplicate Telegram sends for reminders/proactive events
**Where:** `agents/proactive_reaper.py` — 15-24  

**Problem:** STALE_THRESHOLD_SECONDS=60 (proactive_reaper.py:15); reap_stale_reservations uses cutoff = now-60s (line 23). A row created 45s before crash is outside the cutoff and stays 'reserved'. proactive_event_dedup_hit checks status='sent' only, so fire_due_reminders sees no 'sent' row, the reminder is still 'active', and a second message dispatches. Duplicate Telegram message for any reminder/event where Telegram send succeeded, the process crashed before the status='sent' commit, and restart happened within 60s — silently, no ERROR log. Prior ops review Lane 6 P1 flagged 'proactive reservation is not crash-safe after Telegram delivery' (proactive_gate.py:210/247, proactive_reaper.py:18) — still open.

**Fix:** Lower STALE_THRESHOLD_SECONDS to ~10s (normal reserve+send is <3s) to make fast-restart reaping safer, OR in proactive_event_dedup_hit also treat status='reserved' rows younger than N seconds as a soft dedup hit.

### [P2][KNOWN-OPEN] Graph read failure masked by DEBUG-level swallow in recall.py + graph.py with no source-attribution metric
**Where:** `tools/memory/recall.py + storage/graph.py` — recall.py:84-93 ; graph.py:326-328  

**Problem:** recall() wraps _graph.search in try/except logging at logger.debug (recall.py:87) then falls back to legacy. storage/graph.py:327-328 already swallows the Kuzu exception (logger.exception('graph.search failed'); return []), so recall never sees the exception — it gets [] and treats it as 'graph returned nothing', falling back at line 90-93 (also DEBUG). The P0 produces one observable signal (graph.py:327 ERROR ~23/day); recall reports normal operation with no metric/counter for graph-vs-legacy answer source. NOTE (why UNCERTAIN): health.py:126 _check_graphiti_reachable runs a 1-result canary search through the same broken cosine path, so health may already be non-green via that check — the 'ran fully undetected' framing is partially overstated, but there is still no recall-fallback counter. Prior ops review Lane 13 P1 'graph health ignores disabled/transient graph semantics' (health.py:130, graph.py:318/264) is the adjacent open item; the recall-fallback-counter angle is new.

**Fix:** Add counted signals: in recall.py increment runtime_state recall_graph_fallback_count / recall_graph_hit_count on each branch and expose the ratio in agents/health.py alongside graphiti_reachable; bump a graph_search_error counter at storage/graph.py:327 so health turns non-green when graph reads fail even though get_graph() still initializes.

### [P3][KNOWN-OPEN] Second consecutive live-client failure leaves a poisoned non-None client cached; next user turn is burned self-healing it
**Where:** `agents/runtime.py` — 850-879  

**Problem:** _invoke_sdk_persistent_live catches (TimeoutError, ProcessError, CLIConnectionError), reconnects once via _reconnect_live, then calls _run_one() exactly once more (runtime.py:877). If that retry also raises, the exception propagates and _maybe_schedule_live_recycle (879) is skipped while _reconnect_live has already set _live.client to the new (now-dead) client. The next turn's get_live_client returns that non-None corpse instead of cold-connecting, so query() fails and triggers the in-turn reconnect-and-retry again — the following turn pays a failed query()+reconnect before succeeding. Prior ops review Lane 11 P1 'persistent SDK retry can leave a poisoned live client after the second failure' (runtime.py:845, sdk_pool.py:222) — still open.

**Fix:** Wrap the second _run_one() so on a second failure it forces sdk_pool._live.client = None before re-raising: `except (TimeoutError, ProcessError, CLIConnectionError): import agents.sdk_pool as p; p._live.client = None; raise`. Then get_live_client() next turn unconditionally cold-connects.

### [P3][NEW] current_comfort_mode() has a delete side-effect — not safe as a read-only getter
**Where:** `agents/mode_dispatch.py` — 87-89  

**Problem:** When turns_remaining <= 0, current_comfort_mode() calls db.runtime_set(_COMFORT_KEY, None) (mode_dispatch.py:88) and returns None. Calling it twice in different code paths on the same turn yields different results — the first caller sees expired-but-present state, the second sees None. hooks.py:949 calls it from _format_mode_flags; any new caller (e.g. sticker suppression) will silently see None. The side-effect is invisible in the signature (-> dict | None).

**Fix:** Remove the side-effect from current_comfort_mode(). Add a separate expire_stale_comfort_mode() called from decrement_comfort_turn() when it zeroes out and from startup; keep the getter purely read-only.

### [P3][KNOWN-OPEN] memory_prune and monthly_prune both scheduled at day=1 04:00 — concurrent mixed sync/async SQLite writes
**Where:** `agents/scheduler.py` — 254-256 and 411-416  

**Problem:** memory_prune: CronTrigger(day=1, hour=4, minute=0), id='memory_prune' (scheduler.py:255), runs the sync _run_memory_prune in the default thread pool. monthly_prune: CronTrigger(day=1, hour=4, minute=0), id='monthly_prune' (413), async on the event loop. Both write SQLite simultaneously on the 1st at 04:00. WAL keeps it corruption-safe but forces write serialization; on slow/iCloud-synced disks both can stall. The dev explicitly offset weekly_consolidation to 04:30 (comment at line 369) but never offset monthly_prune. Prior ops review Lane 6 P2 'monthly DB-heavy prune jobs both run at day 1 04:00' (scheduler.py:247,405) — still open.

**Fix:** Shift monthly_prune by a couple minutes: change CronTrigger(day=1, hour=4, minute=0) to minute=2 at scheduler.py:413, mirroring the weekly_consolidation separation.

### [P3][KNOWN-OPEN] No periodic cleanup of stuck 'reserved' rows — reap_stale_reservations() is boot-only, orphaning rows on mid-send hangs
**Where:** `agents/proactive_reaper.py + agents/scheduler.py` — reaper called only at telegram_bridge.py:3414  

**Problem:** reap_stale_reservations() is called exactly once, at boot (telegram_bridge.py:3414); no scheduler job re-runs it. A process that hits a multi-minute Telegram flood-wait (429) inside send while holding the reservation leaves that row 'reserved' indefinitely. proactive_event_dedup_hit checks status='sent' only so dedup isn't blocked, but 'reserved' rows accumulate unbounded and pollute audit queries/the /status health report. Prior ops review Lane 6 P1 cluster on reservation crash-safety touches this; the boot-only-reaper-with-no-periodic-job angle is the specific gap.

**Fix:** Add a periodic job in build_scheduler() (e.g. IntervalTrigger(minutes=10)) calling proactive_reaper.reap_stale_reservations() with a grace of ~300s so hung sends are caught without reaping legitimately slow ones.

### [P3][NEW] Deprecated _ebbinghaus_multiplier left in the hot recall module with a vague 'remove after one release' note
**Where:** `storage/retrieval.py` — 334-362  

**Problem:** _ebbinghaus_multiplier (retrieval.py:334) carries an inline 'DEPRECATED — Phase M, kept for rollback / Remove after one release' comment, is no longer called from legacy_retrieve (which uses _act_r_activation), and grep shows no live caller outside tests. Dead code in the core retrieval path invites accidental re-wiring and confuses which decay model is live. Low risk, taste.

**Fix:** Delete _ebbinghaus_multiplier and its docstring; repoint or delete any test that imports it. If a rollback hedge is genuinely wanted, gate it behind a config flag with an owner+date, not a vague 'one release' note.

---
## security-tools  (34: P1=15 P2=14 P3=5)

> The dominant theme is control-plane lies: gates, filters, status surfaces and config knobs that advertise enforcement the runtime never performs. The headline cluster is the tool-governance gate fabric — skill_create/skill_approve/run_skill are an ungated self-approve-and-execute chain (gate:null), the hikari_utility wildcard fails open on new write tools, gate/access_mode typos are unvalidated (fail-open on the exact field whose only job is fail-closed), confirm_send approvals are orphaned at restart while /status and /approvals disagree, and the log-redaction + canary filters are attached to the root logger so they never run on any child-logger record — secrets and the exfiltration canary land in logs cleartext. The external-MCP surface (lowest trust) compounds this: full OAuth tokens stored plaintext, the public-host allowlist reads the wrong config key (every external call 421s under the documented deployment), the passphrase limiter keys on a tunnel-collapsed 127.0.0.1, /register is unbounded, and direct MCP sessions have no timeout and never evict on error. Most of these were already flagged by the prior ops/first reviews and remain unfixed (KNOWN-OPEN); the genuinely NEW findings are second-order defeats of the same controls — the canary deep-walk bypass, the canary re-embed in its own alert, summarize() hiding write payloads from the owner, and the keychain grant item surviving revoke. One prior-style finding (_ACCOUNT_ERRORS "duplicate") is a false positive and was dropped: the two strings differ by a straight vs curly apostrophe and are working as intended.

### [P1][KNOWN-OPEN] skill_create → skill_approve → run_skill is an ungated self-approve-and-execute chain (gate:null) with full tool access
**Where:** `config/tools.yaml` — 927-967 (all three gate:null); tools/skills/core.py:166 (write_text), 188-204 (run_skill→run_internal_control)  

**Problem:** skill_create (927), skill_approve (941), run_skill (955) all have gate:null + access_mode:write. Because the entries are explicit, the wildcard-write deny at gatekeeper_can_use_tool.py:274 never fires, and gatekeeper_can_use_tool.py:286 returns Allow for any non-gatekeeper/confirm_send gate. skill_approve writes attacker/LLM-authored markdown to .claude/skills/<id>/SKILL.md with no owner gate; run_skill feeds that markdown into run_internal_control(prompt, max_turns=8) which uses _base_allowed_tools() — the FULL allowlist (gmail/notion/drive/dispatch). Within one turn the model can author, self-approve, and execute a skill that persists on disk and re-runs every future session. The 'approve' label implies owner consent that is never collected — a control-plane lie. Verified: no out-of-band human-confirm intercept exists in telegram_bridge/background_listener/gatekeeper. Prior first-review backlog Phase 0 #1 flagged this exactly; still gate:null.

**Fix:** Fix: in config/tools.yaml set `gate: gatekeeper` on mcp__hikari_utility__skill_approve (941-953) AND mcp__hikari_utility__run_skill (955-967). Add a per-tool case in tools/gatekeeper.py:summarize() for both rendering skill_id + content preview. Enforce in tools/skills/core.py:skill_approve that the staged session_scratch row was created in a prior, separate turn (reject same-turn create+approve). Add a CI assert in scripts/validate_tool_registry.py that no skill tool is gate:null.

### [P1][KNOWN-OPEN] Full OAuth 2.1 access/refresh tokens stored plaintext in oauth_tokens.token
**Where:** `storage/db.py` — 4564-4580 (oauth_token_mint), 4614-4630 (_oauth2_token_validate)  

**Problem:** oauth_token_mint inserts the raw 256-bit token into oauth_tokens.token unhashed; _oauth2_token_validate does a plaintext WHERE token = ? compare. The parallel hashed path (oauth_token_validate, db.py:4583+) stores only sha256 and its docstring says the plaintext is never persisted — the full-OAuth path is the odd one out, and it is the path claude.ai/iPhone actually use over the tunnel (launch.py AuthMiddleware path 2b). One DB read (backup leak, stolen disk, a SELECT * in a log) hands an attacker live bearer + 30-day refresh tokens for the external MCP. Prior ops review Lane 1 flagged this (P2); still plaintext.

**Fix:** Fix: store only hashlib.sha256(tok).hexdigest() in oauth_tokens (rename column to token_hash), return plaintext to the caller once. Hash the incoming token before WHERE token_hash = ? in _oauth2_token_validate, oauth_token_consume_refresh, oauth_token_revoke_family, and store parent_token as parent_token_hash. Add a migration dropping the plaintext column. Use secrets.compare_digest on the hash confirm.

### [P1][KNOWN-OPEN] server.py allowed_hosts reads legacy public_base_url, ignores PUBLIC_BASE_URL env — external requests get 421
**Where:** `mcp_external/server.py` — 143-159 (build_server)  

**Problem:** build_server only consults cfg.get('mcp_external.public_base_url') to extend allowed_hosts, but config migrated to public_base_url_env: PUBLIC_BASE_URL and leaves the legacy key unset. oauth.py:_public_base_url and launch.py:_public_base_url both honor public_base_url_env first; server.py never does. With DNS-rebinding protection on and allowed_hosts limited to 127.0.0.1/localhost, any request carrying the real Cloudflare Host header is rejected 421. OAuth completes (mounted on the parent Starlette) but every /mcp tool call 421s — the connector 'authorizes' then fails every call under the documented deployment. Prior ops review Lane 1 flagged the host split (P1); still split.

**Fix:** Fix: in build_server resolve via the same precedence as oauth.py — `env_key = cfg.get('mcp_external.public_base_url_env'); public_base_url = (os.environ.get(env_key) if env_key else None) or cfg.get('mcp_external.public_base_url')` — then keep the existing urlparse → allowed_hosts/allowed_origins extension. Factor resolution into one helper shared by server.py, oauth.py, launch.py.

### [P1][KNOWN-OPEN] Passphrase rate limiter keys on request.client.host — behind the tunnel always 127.0.0.1, so it is global and self-DoSing
**Where:** `mcp_external/oauth.py` — 134-136 (_client_ip), 476-482 (_authorize_post)  

**Problem:** _client_ip returns only request.client.host with no CF-Connecting-IP / X-Forwarded-For read. The deployment is Cloudflare Tunnel → 127.0.0.1 (behind_tls_proxy:true), so every /authorize POST arrives as 127.0.0.1. passphrase_limiter (5 attempts/300s) is keyed on that single value for all callers: (1) no per-attacker isolation, and (2) any 5 wrong passphrases lock out the legitimate owner for 300s repeatedly. Audit rows record 127.0.0.1, destroying attribution. Prior ops review Lane 1 flagged forwarded-IP ignored (P2); still ignored.

**Fix:** Fix: in _client_ip, when behind a trusted proxy read CF-Connecting-IP first, then left-most X-Forwarded-For, then fall back to request.client.host. Gate on a config flag (mcp_external.trusted_forwarded_ip) only honored when behind_tls_proxy is true. Use the resolved IP for both the limiter key and the oauth_audit ip field.

### [P1][KNOWN-OPEN] Open /register has no rate limit and no client cap — unbounded unauthenticated row growth
**Where:** `mcp_external/oauth.py` — 281-314 (register_client); launch.py:130-134 (prefix bypass)  

**Problem:** register_client does db.oauth_client_register with no rate-limit check; /register is in OAUTH_PATH_PREFIXES which AuthMiddleware bypasses unauthenticated. db.oauth_client_register does an unconditional INSERT with no COUNT/cap, and combined with the 127.0.0.1 IP collapse an IP limiter alone would not help. Any unauthenticated caller reaching the tunnel hostname can loop POST /register, bloating oauth_clients and flooding oauth_audit_log with attacker-controlled client_name/redirect_uris — disk-fill/table-bloat DoS on the single-user SQLite DB. Prior ops review Lane 1 flagged /register open (P2); still open.

**Fix:** Fix: add an IP-keyed RateLimiter to register_client (mcp_external.oauth.register_max_attempts default 10 / register_window_seconds 3600) after the forwarded-IP fix. Enforce a hard ceiling in oauth_client_register: SELECT COUNT(*) FROM oauth_clients and raise above a configured max (e.g. 50). Prune clients with no successful token issuance older than N days in the daily maintenance job.

### [P1][KNOWN-OPEN] Direct MCP call never evicts a poisoned session on error and has no per-call timeout — one crash wedges the server until restart
**Where:** `agents/mcp_manager.py` — 204-245 (McpManager.call); 226-229 (no timeout); 230-231 (error path leaves session)  

**Problem:** On session.call_tool failure the except (230-231) raises McpCallError but leaves handle.session populated; TTL eviction only governs the warm-pool dict, never the _sessions cache or _exit_stack.aclose(). A dead subprocess/reset connection stays cached and every later call reuses the broken ClientSession and fails identically forever. Separately, await session.call_tool has no asyncio.wait_for, so a hung server blocks the awaiting coroutine and any concurrent caller serialized on handle._lock indefinitely — no deadline, no surfaced error. mcp_introspect.py:68 already bounds MCP I/O; that discipline was not applied to the runtime path. One transient crash on google_workspace/github permanently breaks calendar sync, gcal push, drive/notion producers. Prior ops review Lane 1/11 flagged both (P1); still absent.

**Fix:** Fix: wrap the call as `result = await asyncio.wait_for(session.call_tool(tool_name, arguments=arguments), timeout=self._call_timeout_for(server_name))` reading a per-server timeout from config/tools.yaml (default 30s). In the except branch (catch Exception and asyncio.TimeoutError) tear down the dead session under handle._lock — aclose _exit_stack (guarded by its own try/except), set handle.session = None and handle._exit_stack = None — then raise McpCallError so the next call respawns.

### [P1][KNOWN-OPEN] confirm_send approvals orphaned at restart (never expired, never listed) — /status and /approvals disagree
**Where:** `tools/gatekeeper.py` — config/tools.yaml:2775-2843 (apple_events gate:confirm_send); storage/db.py:3771-3789; agents/telegram_bridge.py:2248; agents/cockpit.py:461  

**Problem:** The apple_events tools use gate:confirm_send and route through GATEKEEPER.request() writing rows with gate_kind='confirm_send'. But db.approval_expire_stale and db.approvals_list_pending_gatekeeper both filter gate_kind='gatekeeper' only (verified in storage/db.py:3773/3787), so a pending confirm_send row is never expired and never nudged; the SDK tool_use_id is gone after restart so the await can never resolve — the row stays pending forever. /approvals (telegram_bridge.py:2248) also filters gate_kind='gatekeeper' so the owner can't see/cancel it, while cockpit /status counts WHERE status='pending' with no gate_kind filter — reporting a phantom pending /approvals will never list. Prior ops review Lane 7 + Lane 14 flagged both halves (P1); still split.

**Fix:** Fix: in storage/db.py change approval_expire_stale and approvals_list_pending_gatekeeper to gate_kind IN ('gatekeeper','confirm_send'). Widen the /approvals WHERE clause in telegram_bridge.py:2248 to the same set. Correct the misleading comment at config/tools.yaml:2770 — confirm_send DOES drive the gatekeeper state machine.

### [P1][KNOWN-OPEN] No enum validation of gate / access_mode at registry load — a one-char typo silently disables a gate
**Where:** `tools/_tools_yaml.py` — 301-327 (_parse_tool)  

**Problem:** _parse_tool reads gate=raw.get('gate') and access_mode=raw.get('access_mode') with no membership check (verified: only a presence check exists, for wildcard access_mode). Downstream gatekeeper_can_use_tool.py:286 does `if gate not in ('gatekeeper','confirm_send'): return Allow`, so a misspelled gate (e.g. gatekeper) makes an intended-gated tool run ungated; a misspelled access_mode (wrtie) on a wildcard misses the access_mode in {write,destructive} deny at line 274 and is allowed. auth_precheck.py validates its mode enum and falls back safely — this path does not. Fail-open on the exact field whose only job is fail-closed. Prior ops review Lane 7 flagged 'registry typos fail open' (P1); still unvalidated.

**Fix:** Fix: in tools/_tools_yaml.py:_parse_tool add `if raw.get('gate') not in (None,'gatekeeper','confirm_send'): raise ValueError(...)` and `if access_mode not in (None,'read','write','destructive'): raise ValueError(...)`. Define the valid sets as module constants reused by both checks.

### [P1][KNOWN-OPEN] Log redaction + canary filters are attached to the root LOGGER, never run on child-logger records — secrets and canary hit logs cleartext
**Where:** `agents/log_scrub.py` — 109-115 (install_root_filter); telegram_bridge.py:3340-3346, mcp_external/launch.py:258-262  

**Problem:** install_root_filter() does root.addFilter(RedactingFilter()) and root.addFilter(CanaryAlertFilter()). In Python logging a Filter on a logger is only consulted in Logger.handle() for records logged DIRECTLY to that logger; records from child loggers (every module uses logging.getLogger(__name__)) propagate only to the root's HANDLERS, whose filters are never the logger's. The RotatingFileHandler/StreamHandler in both main() entrypoints have zero filters attached (grep repo-wide: no handler.addFilter, no LogRecordFactory). So sk-/ghp_/Bearer/OAuth secrets, the Telegram bot token, and the injection canary reach data/logs/hikari.log and stderr unredacted, and the canary CRITICAL escalation never fires from any real path. Prior first-review backlog Phase 0 #3 demanded handler-level safe logging; still logger-level only.

**Fix:** Fix: in install_root_filter() attach both filters to the HANDLERS — `for h in logging.getLogger().handlers: if not any(isinstance(f, RedactingFilter) for f in h.filters): h.addFilter(RedactingFilter())` (same for CanaryAlertFilter). It is already called after addHandler in both entrypoints. Add a regression test: a child logger emits a secret and the file/stderr output is redacted.

### [P1][KNOWN-OPEN] Scope cache not cleared on revoke — stale broad scopes survive a narrower re-grant for 24h
**Where:** `auth/google.py` — 232-249 (revoke), 148-154 (current_scopes)  

**Problem:** GoogleProvider.revoke() calls self._store.clear('google') but never deletes auth.google.scopes or auth.google.scopes_checked_at from runtime_state. current_scopes() trusts the 24h cache without re-probing. After revoking a full-scope grant and re-granting calendar-only, the precheck at agents/hooks.py:1202 still sees the old broad scope set for up to 24h, so a gmail.modify send that should fail scope enforcement passes. Prior ops review Lane 2 flagged stale scope cache across grant/revoke (P2); still not flushed.

**Fix:** Fix: in revoke() after self._store.clear('google') add `from storage import db; db.runtime_set('auth.google.scopes', None); db.runtime_set('auth.google.scopes_checked_at', None)`. Add the same two lines to write_grant_to_keychain() so a script re-grant also flushes the cache.

### [P1][NEW] KeychainStore.clear('google') does not delete the grant keychain item — revoke leaves credentials dangling
**Where:** `auth/store.py` — 77-82 (clear)  

**Problem:** KeychainStore.clear(provider) iterates a hardcoded list ('client_id','client_secret','refresh_token','access_token'), but write_grant_to_keychain() in auth/google.py:54 stores an additional key _GRANT_KEY='grant'. After scripts/auth.py google revoke, read_grant_from_keychain() still returns the revoked token blob; _google_status reports the grant present, and on restart before a new grant GoogleProvider._creds() can read stale revoked credentials from keychain. The prior ops review noted scope-cache staleness on revoke but did not catch that the grant blob itself survives — distinct gap, more severe (live token blob, not just a cache).

**Fix:** Fix: add 'grant' to the list in auth/store.py:KeychainStore.clear() → `for key in ('client_id','client_secret','refresh_token','access_token','grant')`.

### [P1][KNOWN-OPEN] Recurring reminders: GCal/Apple mirrors drift permanently after every recurrence fire
**Where:** `agents/proactive.py` — 448 (recurrence path); 259 (action-reminder reschedule)  

**Problem:** In fire_due_reminders() the recurrence path calls db.reminder_update_fire_at(row['id'], next_due.isoformat()) (verified line 448) but never db.reminder_requeue_sync(). reminders_pending_gcal_sync() only picks rows where gcal_sync_pending=1 (cleared to 0 by the initial sync), so after each fire the GCal/Apple mirror stays on the previous fire time. Only snooze.py:37 calls reminder_requeue_sync. Every recurring reminder's mirror is permanently stuck on its first-ever fire time. Prior ops review Lane 3 flagged recurring mirrors not synced (P1); still missing the requeue.

**Fix:** Fix: after db.reminder_update_fire_at on agents/proactive.py:448 add db.reminder_requeue_sync(row['id']). Apply the same after the action-reminder reschedule at line 259.

### [P1][KNOWN-OPEN] Snooze creates duplicate orphaned GCal events instead of updating the existing one
**Where:** `tools/reminders/sync_gcal.py` — 91-110 (_sync_gcal_reminder)  

**Problem:** _sync_gcal_reminder unconditionally calls create_calendar_event regardless of whether gcal_event_id is already set (verified). reminder_requeue_sync (called by snooze.py:37) sets gcal_sync_pending=1 when gcal_event_id IS NOT NULL, then _sync_gcal_reminder creates a SECOND event and overwrites gcal_event_id, abandoning the original in Google Calendar. Every snooze leaks a phantom GCal event at the old time; repeated snoozes pile up duplicates. Prior ops review Lane 3 flagged snoozed reminders recreated not updated (P1); still create-only.

**Fix:** Fix: in _sync_gcal_reminder look up db.reminder_get(reminder_id) first; if row['gcal_event_id'] is non-null call google_workspace/update_calendar_event with event_id=row['gcal_event_id'], else create. Apply the same pattern to sync_apple.py for Apple Reminders.

### [P1][KNOWN-OPEN] photo_in.enabled config key is defined but never read — all inbound photo handlers are unconditional
**Where:** `agents/telegram_bridge.py` — 889-965 (handle_photo + EXIF/classify), 1397/1609 (_try_ingest_document_photo)  

**Problem:** handle_photo and the EXIF/classify paths fire unconditionally. photo_in.enabled is defined in config/engagement.yaml:824 and documented as a gate, but grep confirms cfg.get('photo_in.enabled', ...) is never called anywhere in the codebase. handle_document for image MIMEs runs _try_ingest_document_photo (EXIF + Nominatim) regardless. Operators who set photo_in.enabled:false still get every photo routed to Anthropic vision and Nominatim geocoding — a control-plane lie. Prior ops review Lane 8 flagged this dead switch (P1); still unread.

**Fix:** Fix: at the top of handle_photo (after the owner_id check ~899) add `if not cfg.get('photo_in.enabled', True): return`. Add the same guard at the top of _try_ingest_document_photo (~1397), and gate the _try_ingest_document_photo call in handle_document (~1609) on cfg.get('photo_in.enabled', True).

### [P1][KNOWN-OPEN] Inbound voice files (data/user_voice/*.ogg) are never deleted — unbounded retention of personal audio
**Where:** `agents/telegram_bridge.py` — 1010-1090 (handle_voice)  

**Problem:** handle_voice downloads the OGG to voice_dir/fname and calls transcribe_voice(abs_path) but never unlinks it on success, failure, or politeness refusal (verified: no unlink in the handle_voice body; the unlinks at lines 206/283/434 are outbound paths). No scheduler job prunes data/user_voice/; _monthly_prune_job only prunes DB tables. Every inbound voice note persists forever — a retention/privacy problem for a personal companion. Prior ops review flagged raw-media-no-TTL generically (Lane 8/9 P2); the inbound-voice-never-deleted specific is the sharper instance.

**Fix:** Fix: after transcribe_voice returns and on each early-return failure path, add `try: abs_path.unlink(missing_ok=True)\nexcept OSError: pass`. Alternatively add a weekly cron in agents/scheduler.py deleting data/user_voice/*.ogg older than voice.file_retention_days.

### [P2][NEW] Canary exfiltration hard-deny only scans top-level arg scalars — nested payloads bypass the tripwire
**Where:** `agents/injection_guard.py` — 170-172 (flag_args_with_untrusted_content)  

**Problem:** flag_args_with_untrusted_content builds its scan blob as `" ".join(str(v) for v in args.values() if isinstance(v, str|int|float))` — TOP-LEVEL scalars only (verified). The gatekeeper's only hard-deny canary check (gatekeeper_can_use_tool.py:300-312) calls this. A canary nested one level deep — Notion children=[{...text:<canary>...}], gmail body in a dict, sheets values=[[<canary>]], drive content — never enters the blob, so the hard-deny never fires. The deep-walk that would catch it (_walk_strings in gatekeeper_can_use_tool.py:130) is used only for the soft URL-taint badge. This is the last-resort exfiltration block (the ONLY block for confirm_send tools); an attacker exfiltrates a planted canary through any gated write by nesting it in a structured field. Prior reviews flagged taint/wrapping broadly but not this specific shallow-scan defeat.

**Fix:** Fix: lift gatekeeper_can_use_tool._walk_strings into agents/injection_guard.py and in flag_args_with_untrusted_content replace the shallow args.values() join with `blob = "\n".join(_walk_strings(args))` before the outbound_contains_canary check, so both call sites share the deep walk.

### [P2][KNOWN-OPEN] External-wrap PostToolUse hook fails OPEN on wrap exceptions — raw untrusted content reaches the model
**Where:** `agents/external_wrap_hook.py` — 256-263 (wrap_post_tool_use except)  

**Problem:** In wrap_post_tool_use, _wrap_tool_response is in try/except; on ANY exception it logs 'passing through raw' and returns {} (verified). An empty dict means no hookSpecificOutput.updatedToolOutput, so the SDK delivers the ORIGINAL unwrapped tool_response to the model. For a tool already matched as untrusted (google_workspace/notion/web patterns), an exception in wrapping — e.g. a malformed/huge content block an attacker can shape — silently disables the only structural prompt-injection defense for that payload. The module docstring calls wrapping 'the load-bearing defense', so failing open contradicts the posture. Prior first-review backlog Phase 1.5 demanded fail-closed wrapping; still returns {}.

**Fix:** Fix: on wrap failure fail CLOSED — in the except branch build `updated = {'content': [{'type':'text','text': f'[untrusted output from {tool_name} suppressed: wrap failed]'}]}` (or the bare-string equivalent) and return it via hookSpecificOutput.updatedToolOutput. Never return {} for a tool that matched a wrap pattern.

### [P2][KNOWN-OPEN] New auto-discovered hikari_utility WRITE tools fail OPEN — wildcard is access_mode:read, validator exempts utility
**Where:** `config/tools.yaml` — 1000-1012 (mcp__hikari_utility__* wildcard); scripts/validate_tool_registry.py:78-82  

**Problem:** The mcp__hikari_utility__* catch-all is gate:null + access_mode:read (verified). The wildcard-write deny (gatekeeper_can_use_tool.py:274) only fires for access_mode in {write,destructive}; a read wildcard returns Allow. validate_tool_registry.py:82 explicitly exempts every uncovered handler starting with mcp__hikari_utility__ from the 'no yaml registration' error (verified). So the documented drop-a-folder-and-restart flow means a new utility write tool with no explicit entry resolves to the read wildcard, runs ungated, and CI passes clean. The fail-closed guarantee the yaml comments claim is false for hikari_utility. Latent (all current utility write tools have explicit entries) but live the moment a new one is added. Prior ops review Phase 1 #1 demanded fail-closed utility tools; still read wildcard.

**Fix:** Fix: change mcp__hikari_utility__* to access_mode:write (matching the google_workspace/notion/github wildcards) so unregistered utility tools hit the wildcard-write deny; the explicit read entries already take precedence. Remove the `not n.startswith('mcp__hikari_utility__')` exemption in scripts/validate_tool_registry.py so genuinely-new utility tools force an explicit yaml decision.

### [P2][KNOWN-OPEN] Invalid/mismatched resource indicator silently mints an unusable token (no aud) that the runtime then 401s
**Where:** `mcp_external/oauth.py` — 378-389 (_normalize_resource), 402/528 (authorize); launch.py:178-194  

**Problem:** _normalize_resource returns None for any resource failing _valid_http_url with no error surfaced. If the client sends a non-parseable resource (or none — aud is only set when resource is present) _encode_resource_into_scope appends no aud, then launch.py:178-185 rejects any access token with no aud binding. A client that sends a resource the validator dislikes completes the entire authorize+token dance, receives a 200 with tokens, then 401s on the first /mcp call with the reason buried in a server-side log.warning — the OAuth endpoints report success while the runtime silently rejects. Prior ops review Lane 1 flagged 'OAuth can mint tokens runtime rejects' (P1); still silent.

**Fix:** Fix: make audience binding explicit at issuance — in token() authorization_code grant, if effective_resource is None bind aud to the server's own public base URL instead of leaving it unset. If a resource is supplied but invalid, return an OAuth invalid_target error from /token rather than silently dropping it. Surface the middleware aud-mismatch as a structured 401 body (error=invalid_token) per RFC 6750.

### [P2][KNOWN-OPEN] validate_mcp_servers treats any error containing the initialize-marker substring as a soft pass — drift false negative
**Where:** `scripts/validate_mcp_servers.py` — 59 (_INITIALIZE_ERROR_MARKER), 74-82 (classification)  

**Problem:** Classification is `is_initialize_error = _INITIALIZE_ERROR_MARKER in err_str` — a substring match against the human string 'MCP server did not respond to initialize' (verified). A server that crashes during initialize for a code reason (broken build) is indistinguishable from a credential-gated soft-skip, so genuine breakage is swallowed as a soft pass. The gate that should fail-closed on undeclared tools silently passes — new ungated MCP tools can ship without policy coverage. Prior ops review Lane 1 flagged drift-validator false negatives (P1); still substring-based.

**Fix:** Fix: have list_server_tools raise a typed McpInitializeTimeout for the no-initialize path and a distinct McpProtocolError for downstream failures. In validate_mcp_servers soft-skip ONLY isinstance(result, McpInitializeTimeout) or names in --allow-unreachable; hard-fail everything else. Add a regression test that an initializes-then-errors server hard-fails.

### [P2][KNOWN-OPEN] External read tools pass caller-supplied limit through with no upper clamp — unbounded read amplification across the boundary
**Where:** `mcp_external/server.py` — 162-177 (hikari_recall), 203-230 (hikari_observations), 250-263 (hikari_wiki_search)  

**Problem:** hikari_recall only defaults limit when falsy then passes it straight to recall_tool.handler → graph.search(num_results=limit) / legacy_retrieve(query, limit) with no ceiling. Same unclamped pass-through in hikari_wiki_search and hikari_observations. Defaults are config-driven but the upper bound is enforced nowhere. An authenticated external caller (or token-stealer per the plaintext-token finding) sends limit=10_000_000 and forces a massive graph/SQLite scan + serialization on the single-user DB over the tunnel — read-amplification DoS from the lowest-trust surface. Prior ops review Lane 1 flagged missing limit clamps (P2); still unclamped.

**Fix:** Fix: define `_MAX_LIMIT = int(cfg.get('mcp_external.max_read_limit', 50))` and apply `limit = max(1, min(int(limit) or default, _MAX_LIMIT))` in hikari_recall, hikari_lexicon_top, hikari_observations, hikari_wiki_search before forwarding. Reject non-int input explicitly.

### [P2][NEW] Apple Events / Playwright / YouTube / DuckDB wildcard access_modes are inconsistent — future write tools fail OPEN
**Where:** `config/tools.yaml` — 2922-2934 (apple_events write+gate:null), 2957/2972/2987 (playwright/youtube/duckdb read)  

**Problem:** mcp__apple_events__* is gate:null + access_mode:write (verified) — its comment says 'no gate; low-risk local device' implying unlisted tools are allowed, but a wildcard+write actually DENIES per gatekeeper_can_use_tool.py:274, so the comment is wrong (unlisted apple_events tools are denied, not allowed). Conversely playwright/youtube/duckdb wildcards are access_mode:read, so a future WRITE tool on those servers (duckdb INSERT, playwright file-download) fails OPEN. The access_mode values are set by guesswork, internally inconsistent with their own comments and each other. Not in prior reviews at this granularity.

**Fix:** Fix: set access_mode:write on mcp__playwright__*, mcp__youtube_transcript__*, and mcp__duckdb__* wildcards so any future write tool on those servers fails closed. Correct the mcp__apple_events__* comment to state unlisted apple_events tools are DENIED, not allowed.

### [P2][NEW] summarize() server-prefix fallbacks render write tools with a generic preview, hiding the payload from the owner
**Where:** `tools/gatekeeper.py` — 464-491 (summarize prefix fallbacks)  

**Problem:** For any gated google_workspace/github/notion tool without a dedicated case, summarize() RETURNS a one-line generic string (e.g. 'github op: push_files on owner/repo', 'notion op: API-patch-page') that omits the body/content/files being written (verified). The richer fallback in gatekeeper_can_use_tool.py:_summarize (which shows _CRITICAL_FIELDS like body/content/code in full) is only reached when the per-tool summarize raises NotImplementedError — but these prefix branches RETURN, shadowing the critical-field renderer for exactly the high-blast-radius writes lacking a dedicated case. The owner CONFIRM-SENDs a github push / notion write seeing only the op name and repo, no diff. Not in prior reviews.

**Fix:** Fix: in tools/gatekeeper.py:summarize make the three server-prefix fallback branches raise NotImplementedError instead of returning, so _summarize falls through to the critical-field renderer that shows body/content/files in full. Preferred: delete the generic returns and let the existing NotImplementedError at line 488 trigger the rich fallback.

### [P2][KNOWN-OPEN] note_create has gate:null — LLM writes to iCloud Notes without owner approval
**Where:** `config/tools.yaml` — 664 (mcp__hikari_utility__note_create)  

**Problem:** mcp__hikari_utility__note_create is gate:null; gatekeeper_can_use_tool.py:287 returns Allow for any non-gatekeeper/confirm_send gate. The confirm=True parameter in tools/apple_notes/create.py:45 is documented as a write-intent check but just returns a redirect message — not a gate. The LLM can write any title/body to iCloud Notes silently, and any injected instruction from an untrusted source (fetched URL, email body) reaching the LLM context can write arbitrary content with no confirmation. Taint-tracking only fires for gated tools, so it is never reached here. Prior ops review Lane 3 flagged this (P1, deduped here to P2 to match severity of comparable ungated writes); still gate:null.

**Fix:** Fix: change gate:null to gate:confirm_send for mcp__hikari_utility__note_create in config/tools.yaml. Remove the redundant confirm parameter from tools/apple_notes/create.py once the real gate is in place.

### [P2][KNOWN-OPEN] Provider-returned image URL in _call_flux is fetched without host allowlist — SSRF via compromised OpenRouter response
**Where:** `tools/photos/_shared.py` — 127-130 (_call_flux url branch)  

**Problem:** When OpenRouter returns a url field instead of b64_json, _call_flux does `img = await client.get(item['url'])` with no host validation (verified). A compromised or malicious OpenRouter response can redirect the fetch to an internal address (169.254.169.254 metadata, localhost, LAN) — SSRF via server-side response injection. Prior ops review Lane 8 flagged provider image URLs fetched without allowlisting (P2); still unvalidated.

**Fix:** Fix: before the fetch validate `parsed = urllib.parse.urlparse(item['url']); assert parsed.scheme == 'https' and parsed.hostname and parsed.hostname.endswith(('.openrouter.ai','.cdn.openrouter.ai'))`. Raise ValueError on mismatch so the outer except returns None.

### [P2][KNOWN-OPEN] EXIF-derived Nominatim label is interpolated raw into the LLM prompt without injection_guard wrapping
**Where:** `agents/telegram_bridge.py` — 1667 (location_hint), 1676 (prompt)  

**Problem:** label is the Nominatim display_name string. It goes into `location_hint = f" exif location: {label!r}."` (verified line 1667) then directly into the run_user_turn_blocks prompt — without wrap_untrusted. Text files and HTML do go through wrap_untrusted (lines 1549/1569); the EXIF geocode label does not. An attacker crafts a JPEG whose GPS maps to a Nominatim display_name containing prompt-injection text, which lands inside Hikari's prompt as trusted context. classify.py _sanitize_details is not applied here. Prior ops review Lane 8/9 flagged EXIF location processing and partial wrapping (P1); the wrap gap specifically remains.

**Fix:** Fix: replace line 1667 with `location_hint = f" exif location: {injection_guard.wrap_untrusted('nominatim', label)}." if label else ""`.

### [P2][KNOWN-OPEN] scene_photo and generate_photo maintain separate daily cap counters — combined output exceeds the intended budget
**Where:** `tools/photos/scene.py` — 79-92 (scene counters); tools/photos/_shared.py:86-98 (generate counters)  

**Problem:** scene_photo_send uses keys scene_photos_sent_date/scene_photos_sent_today (verified); generate_photo uses photos_sent_date/photos_sent_today. Each has its own cap (both default 2) and neither checks the other's counter — the agent can send 4 photos/day (2 selfies + 2 scenes) when the operator may intend a combined 2. The generate_photo description presents its cap as the effective daily limit — a control-plane lie about total volume. Prior ops review Lane 8 flagged split caps despite shared-pool comments (P2); still split.

**Fix:** Fix: have scene_photo_send call _photos_sent_today() and _record_photo_sent() from _shared.py instead of its own keys, OR introduce a shared photos_combined_daily_cap config key backed by a single shared counter function used by both tools.

### [P2][KNOWN-OPEN] _google_status() reports expires_at as granted_at and shows requested-not-granted scopes — control-plane lie
**Where:** `scripts/auth.py` — 127 (_google_status), 108 (_google_grant scope)  

**Problem:** _google_status builds `{'granted_at': grant.get('expires_at','unknown'), 'expires_at': grant.get('expires_at','unknown'), ...}` — both fields show the same expiry (verified). The keychain blob has no separate grant timestamp, so the operator cannot tell when the token was issued. The scopes field shows scopes requested at grant time (line 108), not what Google actually returned — a common discrepancy after partial revocation via Google account settings. Prior ops review Lane 2 flagged broader scopes / scope drift (P2); the status mis-report is the related visible symptom.

**Fix:** Fix: write `'granted_at': datetime.now(UTC).isoformat()` into the payload in _google_grant() before write_grant_to_keychain, then display grant.get('granted_at','unknown'). Note in status output that scopes is requested-at-grant and may differ from live tokeninfo.

### [P2][KNOWN-OPEN] Over-broad base Google scopes: full mailbox scope always granted even for Calendar-only setups
**Where:** `scripts/auth.py` — 55-63 (BASE_SCOPES)  

**Problem:** BASE_SCOPES includes both https://mail.google.com/ (full unrestricted Gmail) and gmail.modify — the latter is fully covered by the former per auth/scope_match.py:17-24, so it is redundant. Both are always requested regardless of whether any Gmail tools are enabled, so a user who only needs reminder-to-Calendar sync still gets a full mailbox grant. Token theft then exposes full mailbox read/write; the consent screen also shows the most alarming permission. Prior ops review Lane 2 flagged scopes broader than least privilege (P2); still always-on.

**Fix:** Fix: remove gmail.modify from BASE_SCOPES (covered by mail.google.com/). Make mail scopes optional via --add rather than always-on, or gate them on whether any gmail tools are enabled in config/tools.yaml.

### [P3][NEW] CanaryAlertFilter re-embeds the leaked canary token into its own escalated CRITICAL log message
**Where:** `agents/log_scrub.py` — 100-104 (CanaryAlertFilter.filter)  

**Problem:** On detecting the canary the filter sets `record.msg = '[CANARY LEAK DETECTED] ' + str(record.msg)` — it prepends the tag but keeps the original message, which by definition still contains the canary (that's why it matched), and does NOT redact it. Even when this filter runs (after the root-vs-handler fix), the CRITICAL alert it emits contains the live canary, now tagged so it is more likely to be forwarded to alert sinks / crash reporters / log shipping. The leak-detection mechanism amplifies the leak. Prior first-review backlog Phase 0 #3 said 'never emit raw canary'; the re-embed is the concrete violation.

**Fix:** Fix: redact before re-emitting — in CanaryAlertFilter.filter fetch the canary (db.runtime_get on the key injection_guard uses, guarded by try/except) and do `record.msg = '[CANARY LEAK DETECTED] ' + str(record.msg).replace(canary, '[REDACTED-CANARY]')`. Order/chain RedactingFilter so redaction always runs on the escalated message.

### [P3][NEW] skill_promoter stages skills with an unvalidated, LLM/untrusted-derived skill_id
**Where:** `agents/skill_promoter.py` — 127-146 (maybe_promote_skill)  

**Problem:** maybe_promote_skill reads skill_id straight from the aux-LLM JSON (verified ~line 127) and inserts a session_scratch row keyed staged_skill:<skill_id> with NO _validate_skill_id call (which tools/skills/core.py:45 enforces on the manual path). The aux-LLM input is recent character_thoughts, which can include summaries of untrusted fetched content. The disk write is still safe because skill_approve re-validates, but a hostile path-traversal-shaped id is persisted into session_scratch and surfaced to the model as a ready-to-approve staged-skill name — shortening the path to a persisted malicious skill when combined with the ungated skill_approve finding. Not in prior reviews.

**Fix:** Fix: in skill_promoter.maybe_promote_skill, after extracting skill_id add `from tools.skills.core import _validate_skill_id; if _validate_skill_id(skill_id): _set_cooldown('invalid_skill_id'); return` before the session_scratch INSERT, so staging and approval enforce identical id constraints.

### [P3][NEW] Daily-cap counters use non-atomic read-modify-write — concurrent calls can bypass the cap
**Where:** `tools/photos/_shared.py` — 93-98 (_record_photo_sent); tools/voice_outbound.py:77-83; tools/photos/scene.py:86-92  

**Problem:** _record_photo_sent reads _photos_sent_today() then db.runtime_set('photos_sent_today', count + 1) — two separate SQLite ops (verified). Identical pattern in voice_outbound._bump_sent and scene.py. db.runtime_increment (db.py:3224) exists and is the atomic fix but none of these callers use it. Two concurrent voice_outbound_send / generate_photo calls (e.g. a proactive path firing during a user turn) can both read the same count, both pass the cap, and both write the same value — doubling/skipping cap enforcement. Not in prior reviews.

**Fix:** Fix: replace _record_photo_sent body with db.runtime_increment('photos_sent_today'); same for _bump_sent in voice_outbound.py and the increment in scene.py. Keep the date-rollover guard: check runtime_get('..._date') != today and reset the key before incrementing.

### [P3][NEW] Non-deterministic tool_use_id fallback breaks gatekeeper idempotency when context lacks an id
**Where:** `tools/gatekeeper_can_use_tool.py` — 289  

**Problem:** `tool_use_id = getattr(context, 'tool_use_id', None) or f'missing-{tool_name}-{id(input)}'` (verified). id(input) is the CPython memory address — not stable across calls and reusable after GC. The gatekeeper keys its in-memory pending slot, DB row, and resolve() lookup on tool_use_id. If the SDK ever omits tool_use_id (older/edge builds, tests), the synthesized id is unique per object so the idempotency join can never match a retry (duplicate pending approvals), and a stale missing- id could collide with a later input dict reusing the same address. Latent dedup/correctness bug. Not in prior reviews.

**Fix:** Fix: replace the fallback with a deterministic hash — `tool_use_id = getattr(context,'tool_use_id',None) or 'synth-'+hashlib.sha256((tool_name+json.dumps(input,sort_keys=True,default=str)).encode()).hexdigest()[:24]` so the same input yields the same id and retries join the existing pending slot.

### [P3][NEW] scene_photo.daily_cap read at module import time — config changes show a stale cap in the tool description
**Where:** `tools/photos/scene.py` — 109 (_DAILY_CAP)  

**Problem:** _DAILY_CAP = int(cfg.get('scene_photo.daily_cap', 2)) at line 109 is a module-level assignment evaluated once at import; scene_photo_send re-reads the cap inline at line 126, so _DAILY_CAP is used only in the tool docstring. The LLM-facing description shows the import-time value even after an operator changes the cap at runtime. No incorrect behavior (the runtime check is correct) but the model is misled. Not in prior reviews.

**Fix:** Fix: remove _DAILY_CAP and write a plain-string @tool description without the formatted cap, or compute the cap lazily in the description.

---
## ops-cost  (25: P1=6 P2=13 P3=6)

> The dominant theme is a cost/observability control-plane that systematically lies: nearly every spend path (main chat SDK turns, all ~120 OpenRouter aux calls, research_worker, bounded_rewrite, photo vision, ElevenLabs TTS) is either uncounted, zero-priced, or budget-uncapped, so /cost and daily_cap_remaining() are structurally meaningless. The second cluster is durability/correctness in the storage layer: media_outbox drains have no atomic claim and double-send under the periodic-vs-per-turn interleave, the Kuzu graph (half the long-term memory) is never backed up, foreign keys are silently off in production, and migration gating races across processes and bricks on a docstring edit. A third cluster is config/control drift: 5 world-delta producers are permanently dead (yaml omits them, code fallback is unreachable), retention/budget keys are dead or invisible, and several health/observability surfaces (timezone-skewed error count, CRITICAL canary invisible, stale README thresholds, mislabeled redaction tags) report green during real failures. Every finding cross-checks against the prior 2026-05-28 ops review: the big P1s (aux cost, research_worker, media double-send, Kuzu backup, FK off, migration race, drain wrong-recipient) are all KNOWN-OPEN — still present in current code — and a set of sharper, newly isolated findings are NEW. Nothing was found KNOWN-FIXED; none of the prior conclusions are contradicted.

### [P1][KNOWN-OPEN] _call_aux_llm never records cost — all OpenRouter aux-LLM spend (~120 call sites) is invisible
**Where:** `/Users/ol/agents/hikari-agent/agents/runtime.py` — 461-475  

**Problem:** Prior ops review Lane 10 P1 'OpenRouter aux calls are mostly invisible.' Still open. _call_aux_llm parses payload['choices'] (runtime.py:462) but never reads payload['usage'], which OpenRouter returns on every response. Reflection, diary, stickers, annual_review, skill_promoter, tonal_recall, dialectic, proactive accountability — ~120 call sites — write zero rows to llm_costs. Only drift_judge.py:258 logs one sub-call. The /cockpit rollup undercounts the entire OpenRouter line; even a fixed daily_cap_remaining() would miss it.

**Fix:** In runtime.py after line 461 (payload = resp.json()) and before returning the content at 475, add: usage = payload.get('usage') or {}; pt = usage.get('prompt_tokens'); ct = usage.get('completion_tokens'); _log_aux_cost(model=effective_model, prompt_chars=(pt*4 if pt else len(system)+len(prompt)), completion_chars=(ct*4 if ct else len(message['content'])*1), path='aux_llm') wrapped in try/except so a logging failure never breaks the call. One edit covers all ~120 call sites.

### [P1][KNOWN-OPEN] media_outbox drain has no atomic claim — concurrent periodic + per-turn drains double-send the same row
**Where:** `/Users/ol/agents/hikari-agent/agents/telegram_bridge.py` — 453-468  

**Problem:** Prior ops review Lane 11 P1 'media_outbox drains can double-send.' Still open. _drain_media_outbox reads db.media_outbox_pending(kind=kind) (telegram_bridge.py:463) then dispatches across an await; the row stays status='pending' until mark_sent runs AFTER bot.send_* returns. The 2-min periodic _media_outbox_drain_job (scheduler.py:608-625) and per-turn drains run on the same event loop; max_instances=1/coalesce only guards periodic-vs-periodic. While dispatcher A awaits the send of pending row X, drain B reads the same row X and sends it again. Idempotency keys embed millisecond timestamps so they never dedup a logical resend. proactive_events already uses a reserved-claim pattern; media_outbox is the inconsistent one.

**Fix:** Add an atomic claim. Replace media_outbox_pending in the drain path with a claiming UPDATE ... SET status='sending', attempts=attempts+1 WHERE id IN (SELECT id FROM media_outbox WHERE status='pending' AND kind=? ORDER BY created_at,id LIMIT ?) RETURNING * (SQLite 3.50+ supports RETURNING). Add 'sending' to the status CHECK via table-rebuild migration. mark_failed flips 'sending'->'pending'. Add a stale-'sending' reaper mirroring proactive_events_stale_reserved at storage/db.py:3319.

### [P1][KNOWN-OPEN] Kuzu graph DB (data/hikari.kuzu) is never backed up — long-term memory unrecoverable on disk loss
**Where:** `/Users/ol/agents/hikari-agent/scripts/backup.sh` — 95-111  

**Problem:** Prior ops review Lane 5 P1. Still open. backup.sh snapshots only data/hikari.db via sqlite3 .backup (line 96) and tars hikari.db + .env/secrets (line 111). data/hikari.kuzu (storage/graph.py:40) — the Graphiti entity/relationship graph holding all consolidated long-term memory — is absent from the tar. graph_outbox is the only durability link but sent/drained rows are pruned after 14 days, so older episodes live only in hikari.kuzu. Disk loss silently restores an empty/partial graph with no error.

**Fix:** In backup.sh after the SQLite snapshot (line 96), checkpoint/copy the kuzu DB into TMP_DIR: cp -R "$REPO_DIR/data/hikari.kuzu" "$TMP_DIR/hikari.kuzu" (after a graph checkpoint), then tar --append --file "$TMP_TAR" -C "$TMP_DIR" hikari.kuzu. Extend the verify block at backup.sh:142 to assert the kuzu artifact extracts and opens.

### [P1][NEW] 5 world-delta producers permanently dead — yaml default_enabled_sources omits them, code fallback is unreachable
**Where:** `/Users/ol/agents/hikari-agent/config/engagement.yaml` — 39-44  

**Problem:** Not in prior ops review. yaml proactive.default_enabled_sources lists 5 entries (gmail_unread_threshold, calendar_event_prep, wiki_new_file, decision_resolve_due, reengage_silence). Code DEFAULT_ENABLED_SOURCES (producers/__init__.py:75-85) additionally includes book_just_finished, irritation_event, just_got_home, late_night_dissolution, weather_mood_shift. scheduler.py reads yaml first and only falls back to code when yaml is absent — yaml is always present, so the code fallback is dead. Those 5 producers' collect() functions are implemented but never called in production; the __init__ docstring calling them 'Default-on' is false.

**Fix:** Append book_just_finished, just_got_home, late_night_dissolution, irritation_event, weather_mood_shift to proactive.default_enabled_sources in config/engagement.yaml:44. Note this requires the paired config-block fix below before enabling, or they fire with no rate limit.

### [P1][NEW] log_recent_errors health check compares local-time log timestamps against a UTC epoch cutoff — false-green in UTC-negative zones
**Where:** `/Users/ol/agents/hikari-agent/agents/health.py` — 233-236  

**Problem:** Not in prior ops review (prior only flagged threshold staleness, not the TZ bug). The root formatter at telegram_bridge.py:3327 uses default %(asctime)s = local time with no converter=time.gmtime. _check_recent_log_errors parses the local-time string then attaches tzinfo=UTC (health.py:235), then compares ts.timestamp() against time.time()-3600 (real UTC). In UTC-N zones a recent error looks N hours older than it is, falling before the cutoff and producing a false count of 0. In UTC-positive zones the count is inflated.

**Fix:** Set the logger to UTC: in telegram_bridge.main() before install_root_filter() (telegram_bridge.py:3346) add logging.Formatter.converter = time.gmtime (and the same in mcp_external/launch.py). This makes all logs UTC so the health.py:235 attach-UTC assumption becomes correct.

### [P1][KNOWN-OPEN] README health thresholds are stale and contradict code constants
**Where:** `/Users/ol/agents/hikari-agent/README.md` — 272-274  

**Problem:** Prior ops review Lane 13 P2 (raised to P1 here on incident-response impact). Still open. README:272 says graph_outbox_pending degrades at '< 50' but code _OUTBOX_PENDING_WARN=10 (health.py:41). README:274 says log_recent_errors '≤ 5 ERROR lines/hour' but code _LOG_RECENT_ERRORS_WARN=10 (health.py:44). A responder following the runbook investigates a >10-error situation and wrongly concludes healthy per docs.

**Fix:** Edit README.md:272 to 'graph_outbox_pending | > 10 pending writes' and README.md:274 to 'log_recent_errors | > 10 ERROR/CRITICAL lines in the last hour' to match health.py constants.

### [P2][KNOWN-OPEN] research_worker spawns SDK subprocess with no max_budget_usd cap and never records cost
**Where:** `/Users/ol/agents/hikari-agent/agents/subagents/research_worker.py` — 88-106  

**Problem:** Prior ops review Lane 10 P1. Still open. ClaudeAgentOptions at research_worker.py:88 sets system_prompt/allowed_tools/max_turns/permission_mode/setting_sources but no max_budget_usd, so each task can run all 8 turns uncapped (up to 2 tasks x 2 loops/day). The receive_response loop breaks on ResultMessage (line 105-106) but never reads msg.usage, so no llm_costs row is written.

**Fix:** Add max_budget_usd=float(cfg.get('research_worker.per_task_max_budget_usd', 0.50)) to the ClaudeAgentOptions at research_worker.py:88. In the ResultMessage branch at line 105, before break, record cost via runtime._record_llm_cost(getattr(msg,'model_usage',None), path='research_worker', fallback_model='claude-sonnet-4-6', fallback_usage=getattr(msg,'usage',None)).

### [P2][NEW] bounded_rewrite runs a Sonnet SDK turn with no cost tracking; docstring/config still say Haiku
**Where:** `/Users/ol/agents/hikari-agent/agents/post_filter.py` — 806-828  

**Problem:** Not named in prior ops review. bounded_rewrite builds ClaudeAgentOptions(model='claude-sonnet-4-6', max_turns=1, max_budget_usd=0.01) (post_filter.py:803-812) and runs a ClaudeSDKClient; the receive_response loop (818-822) collects only AssistantMessage TextBlocks — no ResultMessage branch — so msg.usage is never read and llm_costs gets no row. Runs on the drift-rewrite path, potentially several times/day. The docstring (post_filter.py:770) and engagement.yaml:252/312 say 'Haiku' while the model is Sonnet (~10x cost), so rewrite_max_budget_usd:0.01 (yaml:317) under-budgets.

**Fix:** Add to the loop at post_filter.py:822: elif isinstance(msg, ResultMessage): from agents.runtime import _record_llm_cost; _record_llm_cost(getattr(msg,'model_usage',None), path='bounded_rewrite', fallback_model=model, fallback_usage=getattr(msg,'usage',None)). Update the docstring at post_filter.py:770 and comments at engagement.yaml:252,312 to 'claude-sonnet-4-6', and raise rewrite_max_budget_usd to 0.04 at engagement.yaml:317.

### [P2][KNOWN-OPEN] cost_today() always returns 0 — runtime_state key 'cost_today' is never written
**Where:** `/Users/ol/agents/hikari-agent/tools/budget.py` — 34-39  

**Problem:** Prior ops review Lane 10 P2 'daily budget/cockpit status split between old and new accounting.' Still open. cost_today() reads db.runtime_get('cost_today') / 'cost_today_date' (budget.py:36-39); a codebase-wide grep shows neither key is runtime_set anywhere. So cost_today()=0 always, the cockpit 'cost today' chat line is always $0, and daily_cap_remaining()=cap-0-background reports almost the full cap regardless of SDK turns run.

**Fix:** Rewrite budget.cost_today() to query llm_costs live: cutoff = datetime.now(UTC).replace(hour=0,minute=0,second=0,microsecond=0).isoformat(); with db._conn() as c: row=c.execute('SELECT COALESCE(SUM(cost_usd),0) s FROM llm_costs WHERE ts>=?',(cutoff,)).fetchone(); return float(row['s'] or 0.0). Drop the cost_today_date check. Point cockpit.py (chat_today readout near line 479) at budget.cost_today().

### [P2][KNOWN-OPEN] ElevenLabs TTS rate missing from _MODEL_RATES_USD_PER_1M — every voice note logged as $0
**Where:** `/Users/ol/agents/hikari-agent/tools/voice_outbound.py` — 210-219  

**Problem:** Prior ops review Lane 10 P2 'voice/STT costs not tracked' + 'unknown models stored as zero-cost.' Still open and self-documented. voice_outbound.py:211 comment admits ElevenLabs is absent from _MODEL_RATES_USD_PER_1M so _log_aux_cost(model='elevenlabs/flash_v2_5') hits the unknown-model branch and returns $0. Flash v2.5 is ~$0.10/1000 chars (~$0.01-0.02 per reply). TTS spend is structurally invisible in /cockpit.

**Fix:** Either add 'elevenlabs/flash_v2_5': (0.10, 0.0) to _MODEL_RATES_USD_PER_1M in runtime.py (input rate as $/1000-char pseudo-tokens via the existing char//4 path), or in voice_outbound.py compute cost directly: cost_usd = len(text)/1000*0.10 and call db.llm_costs_insert with the correct value instead of relying on the unknown-model branch.

### [P2][KNOWN-OPEN] PRAGMA foreign_keys is never enabled on pooled connections — all REFERENCES / ON DELETE CASCADE unenforced in production
**Where:** `/Users/ol/agents/hikari-agent/storage/db.py` — 606-612  

**Problem:** Prior ops review Lane 5 P1. Still open. _get_pooled_conn runs PRAGMA journal_mode=WAL/busy_timeout/synchronous (db.py:607-612) but never PRAGMA foreign_keys=ON; SQLite defaults FK off per-connection. Grep finds the PRAGMA only in tests (test_foreign_keys.py:46) which document 'Without PRAGMA foreign_keys=ON child rows are orphaned.' Schema declares ON DELETE CASCADE on entity_aliases, fact_entities, work_packet_steps and plain REFERENCES on facts.superseded_by, tasks.blocked_by, accountability_items, oauth_codes/tokens — none enforced live. Latent today (soft-delete only) but tests pass while prod differs.

**Fix:** Add c.execute('PRAGMA foreign_keys=ON') in _get_pooled_conn right after PRAGMA synchronous (storage/db.py:612). Verify the two table-rebuild migrations (graph_outbox, media_outbox) still pass with FKs on — run those rebuilds with foreign_keys temporarily OFF if any rename a referenced parent.

### [P2][NEW] Migration checksum hashes full inspect.getsource() — editing any deployed migration's comment/docstring bricks boot
**Where:** `/Users/ol/agents/hikari-agent/storage/migrations.py` — 30-31  

**Problem:** Not in prior ops review. _checksum_for(fn) hashes inspect.getsource(fn) — entire function source including comments/docstring (migrations.py:31). run_once raises RuntimeError('schema_migrations checksum drift') on mismatch for an already-applied row (migrations.py:68-71), and _ensure_schema (db.py:619) has no try/except, so it propagates out of the first _conn() and crashes boot. Several migration docstrings already carry volatile dated prose (e.g. _migrate_drop_facts_legacy_superseded_by cites a 2026-05-28 date + commit hash). A maintainer editing a docstring cosmetically bricks every already-migrated DB at next boot.

**Fix:** Stop hashing full source. Give each migration an explicit version:int (or stable tag) and pass it as checksum= to run_once (param already exists at migrations.py:39); compare the tag instead of getsource(). Bump the tag only on a real DDL/behavior change.

### [P2][KNOWN-OPEN] Photo classifier (Sonnet vision via direct Messages API) cost never tracked
**Where:** `/Users/ol/agents/hikari-agent/tools/photos/classify.py` — 178-183  

**Problem:** Prior ops review Lane 10 P2 'direct vision classifier costs not tracked.' Still open. _call_vision_api gets body = resp.json() (classify.py:178) which carries the standard usage block (input_tokens/output_tokens) but returns only the first text block (classify.py:180-183); classify_photo_intent discards usage. No _log_aux_cost or llm_costs_insert anywhere in classify.py. Every inbound user photo triggers an untracked Sonnet vision call.

**Fix:** Have _call_vision_api return (text, body). In classify_photo_intent (classify.py:212) extract usage = body.get('usage',{}) and call _log_aux_cost(model='claude-sonnet-4-6', prompt_chars=usage.get('input_tokens',0)*4, completion_chars=usage.get('output_tokens',0)*4, path='photo_classify') inside try/except to preserve the never-raises contract.

### [P2][KNOWN-OPEN] Periodic + boot media_outbox drains use owner_id(), ignoring per-row chat_id — wrong-recipient delivery in any multi-chat scenario
**Where:** `/Users/ol/agents/hikari-agent/agents/telegram_bridge.py` — 453-468  

**Problem:** Prior ops review Lane 8 P1 'Media outbox drains to current chat, not row's intended chat.' Still open. media_outbox payloads carry chat_id (messaging.py) but the dispatchers _send_outbox_photo/text/sticker/document/voice take chat_id as a function arg (telegram_bridge.py:236,293,321,349,394) and never read row payload['chat_id']. _media_outbox_drain_job (scheduler.py:617) and the boot drain pass _owner_id(), so any row enqueued for a non-owner chat that was not drained inline is later delivered to the owner.

**Fix:** Have each dispatcher parse payload_json and honor payload['chat_id'] when present, falling back to the passed chat_id only when absent. Change _drain_media_outbox (telegram_bridge.py:453) to group pending rows by their payload chat_id and dispatch each to its own chat rather than forcing owner_id().

### [P2][NEW] sender.py always calls record_user_anchored_sent regardless of candidate pool — agent_spontaneous sends bypass their tighter cap
**Where:** `/Users/ol/agents/hikari-agent/agents/engagement/sender.py` — 162-167  

**Problem:** Not in prior ops review. sender.py:164 unconditionally calls cadence.record_user_anchored_sent(candidate.source). Producers declaring pool='agent_spontaneous' (irritation_event, book_just_finished, just_got_home) get recorded against Pool.USER_ANCHORED. cadence.record_spontaneous_sent exists but is never called. user_anchored cap is 30/7d, agent_spontaneous is 8/7d — the miscount lets spontaneous sources eat the looser quota and bypass their tighter cap. Compounds the dead-producer fix: once those 5 are re-enabled they fire more than intended.

**Fix:** In sender.py:162-166 resolve the pool (cadence._resolve_pool(candidate.source) or candidate.pool) and dispatch to the matching recorder: record_spontaneous_sent for agent_spontaneous, record_user_anchored_sent otherwise.

### [P2][KNOWN-OPEN] /settings proactive.enabled writer accepts arbitrary source IDs with no validation against ALL_PRODUCER_IDS
**Where:** `/Users/ol/agents/hikari-agent/agents/cockpit.py` — 138-141  

**Problem:** Prior ops review Lane 14 P2 'accepts invalid JSON source lists, scheduler silently ignores unknown IDs.' Still open. cockpit.py:262 validate is lambda v: True; _write_proactive_enabled (cockpit.py:138-141) json.loads(v) and stores to runtime_state with no membership check. scheduler.py skips unknown IDs (get_producer->None), so a typo like 'gmail_unread' saves successfully and silently never fires.

**Fix:** In _write_proactive_enabled after json.loads(v) (cockpit.py:140), import ALL_PRODUCER_IDS from producers and raise: unknown=[s for s in sources if s not in ALL_PRODUCER_IDS]; if unknown: raise ValueError(f'unknown source IDs: {unknown}').

### [P2][NEW] 5 world-delta producers have no engagement.* config section — no min_interval, value floor, or interruption_right
**Where:** `/Users/ol/agents/hikari-agent/config/engagement.yaml` — 1085-1244  

**Problem:** Not in prior ops review. book_just_finished, irritation_event, just_got_home, late_night_dissolution, weather_mood_shift have zero entries under the engagement: section (grep returns no matches). selector.py falls back to min_interval_minutes=0 (no rate limit), min_value_score=0.0 (no value floor), interruption_right='low'. Once the dead-producer finding is fixed and they enable, a weather_mood_shift candidate at score>0 can fire every 60s tick.

**Fix:** Add an engagement: block for each of the 5 producers, e.g. book_just_finished: {enabled: true, priority_tier: 2, min_interval_minutes: 1440, send_mode: proactive, min_value_score: 0.4, interruption_right: low}; tune intervals per producer intent (weather_mood_shift/irritation shorter, late_night_dissolution night-gated).

### [P2][NEW] CRITICAL-level canary leak log lines are invisible to the log_recent_errors health check
**Where:** `/Users/ol/agents/hikari-agent/agents/health.py` — 63  

**Problem:** Not in prior ops review. _ERROR_LINE_RE = re.compile(r'\bERROR\b') (health.py:63) matches only the literal word ERROR. CanaryAlertFilter (log_scrub.py) upgrades injected-canary records to levelname='CRITICAL'; the written line reads 'CRITICAL agents.post_filter: [CANARY LEAK DETECTED]' with no ERROR token, so it is excluded from the count. An active prompt-injection canary fires CRITICAL but the startup/status health probe stays green.

**Fix:** Change health.py:63 to _ERROR_LINE_RE = re.compile(r'\b(ERROR|CRITICAL)\b'). No other change needed; CRITICAL is more severe than ERROR so including it is correct.

### [P2][NEW] decision_log calibration surface bypasses the proactive gate (quiet hours, silence window, dedup)
**Where:** `/Users/ol/agents/hikari-agent/agents/decision_log.py` — 113  

**Problem:** Not isolated in prior ops review. Decision questions use reserve_and_send (enforces quiet hours/silence/dedup), but the calibration surface at decision_log.py:113 calls await send_text(surface) directly (raw send_and_persist, no gate). The block fires unconditionally when n_total>=8 (line 109) at the scheduled 19:00 run, ignoring active quiet hours. Users with 19:00 Sunday quiet hours receive an unsolicited calibration message, violating the silence contract.

**Fix:** Replace send_text(surface) at decision_log.py:113 with reserve_and_send(..., producer_id='decision_log', pattern='ceremony', dedup_key='decision_log:calibration:'+iso_week) so it honors quiet hours/silence and dedups duplicate calibration fires.

### [P3][NEW] post_filter rewrite cost comments say 'Haiku' while config uses claude-sonnet-4-6 — drives wrong cost budget
**Where:** `/Users/ol/agents/hikari-agent/config/engagement.yaml` — 252  

**Problem:** Not in prior ops review. engagement.yaml:252 ('a bounded Haiku turn') and :312 ('One Haiku turn') plus post_filter.py:770 docstring all say Haiku, but rewrite_model is 'claude-sonnet-4-6' (yaml:316) read at post_filter.py:803. An operator setting rewrite_max_budget_usd:0.01 (yaml:317) on Haiku assumptions under-budgets ~10x on Sonnet. Overlaps the bounded_rewrite cost finding above; track the comment/budget edits there.

**Fix:** Edit comments at engagement.yaml:252,312 and the docstring at post_filter.py:770 to 'claude-sonnet-4-6', and raise rewrite_max_budget_usd to 0.04 at engagement.yaml:317.

### [P3][NEW] 4 retention keys silently missing from yaml — scheduler falls back to hardcoded values invisible to operators
**Where:** `/Users/ol/agents/hikari-agent/config/engagement.yaml` — 1076-1078  

**Problem:** Not in prior ops review. scheduler.py:398-404 reads retention.tool_calls_days (30), retention.graph_outbox_sent_days (14), retention.media_outbox_terminal_days (14), retention.proactive_events_days (90). The retention: section in engagement.yaml only declares messages_days, oauth_audit_log_days, calendar_notifications_days (yaml:1076-1078). The 4 keys are absent, so operators cannot tune these windows and the yaml is not the full picture of pruning behavior.

**Fix:** Add to the retention: section in engagement.yaml: tool_calls_days: 30, graph_outbox_sent_days: 14, media_outbox_terminal_days: 14, proactive_events_days: 90 (matching the scheduler defaults).

### [P3][NEW] No startup guard: ANTHROPIC_API_KEY set alongside CLAUDE_CODE_OAUTH_TOKEN silently double-bills the main SDK path
**Where:** `/Users/ol/agents/hikari-agent/agents/telegram_bridge.py` — 3323-3346  

**Problem:** Not in prior ops review. main() (telegram_bridge.py:3323) checks Google Workspace env but never checks whether ANTHROPIC_API_KEY is set. CLAUDE.md mandates never setting it because the SDK falls back to it and double-bills on top of the $200/mo Max subscription. If it leaks into the env (rc file, .env typo, third-party tool), every SDK turn bills at direct API rates with no log warning.

**Fix:** In main() right after load_dotenv() (telegram_bridge.py:3324) add: if os.environ.get('ANTHROPIC_API_KEY') and os.environ.get('CLAUDE_CODE_OAUTH_TOKEN'): logger.warning('DOUBLE-BILL RISK: ANTHROPIC_API_KEY and CLAUDE_CODE_OAUTH_TOKEN both set; SDK may bill pay-per-token on top of the Max subscription. Unset ANTHROPIC_API_KEY.')

### [P3][KNOWN-OPEN] No cross-process migration lock — concurrent first-boot of a stale DB races on non-idempotent ALTER TABLE
**Where:** `/Users/ol/agents/hikari-agent/storage/db.py` — 619-632  

**Problem:** Prior ops review Lane 5 P1 'migration idempotency not safe across concurrent processes.' Still open. Gating uses process-global _SCHEMA_INITIALIZED + _SCHEMA_LOCK=threading.Lock() (per-process only). Bridge plus standalone scripts (backfill_embeddings, backfill_facts_to_graph, ingest_to_memory, reconcile_graph) all import storage.db and trigger _ensure_schema. The 'column missing' check in migrations like _migrate_facts_bitemporal (db.py:705) is itself the race window: two processes both pass it, both ALTER, the second raises 'duplicate column' uncaught, crashing that process.

**Fix:** In _ensure_schema (db.py:623) take a cross-process lock before the migration cascade: conn.execute('BEGIN IMMEDIATE') (grabs the WAL write lock; busy_timeout already set) held through _migrate_tasks_decay_columns, commit at end — a second process blocks then sees columns already present. Alternatively wrap every ALTER in the duplicate-column try/except the reminders migrations already use.

### [P3][NEW] budget.call_window_sec and budget.call_window_max are dead config keys with zero code readers
**Where:** `/Users/ol/agents/hikari-agent/config/engagement.yaml` — 104-105  

**Problem:** Not in prior ops review. engagement.yaml:104-105 define budget.call_window_sec:300 and budget.call_window_max:30. Grep across agents/storage/tools/config returns zero readers; tools/budget.py reads only budget.daily_cap_usd_env and budget.daily_cap_usd_default. The operator believes a per-window call throttle is enforced; it does not exist.

**Fix:** Remove budget.call_window_sec and budget.call_window_max from engagement.yaml:104-105 (no rate-limiter consumes them), or implement the window throttle in tools/budget.py that reads them.

### [P3][NEW] log_scrub pattern ordering: sk-ant-/sk-or- keys matched by the generic sk- pattern, producing mislabeled redaction tags
**Where:** `/Users/ol/agents/hikari-agent/agents/log_scrub.py` — 24-26  

**Problem:** Not in prior ops review. _PATTERNS applies sk-[a-zA-Z0-9_-]{20,} first (log_scrub.py:24) before sk-ant- (25) and sk-or- (26). The generic pattern consumes all sk- tokens on first pass; specific patterns never fire, so sk-ant-... redacts to [REDACTED-API-KEY] not [REDACTED-ANTHROPIC-KEY]. Secret is still redacted (no exposure) but SOC/audit searches for [REDACTED-ANTHROPIC-KEY] return nothing even when Anthropic keys leaked.

**Fix:** Reorder _PATTERNS in log_scrub.py so sk-ant- (line 25) and sk-or- (line 26) precede the generic sk- (line 24); generic last.

---
## delivery-evals  (28: P1=8 P2=12 P3=8)

> The dominant theme is control-plane lies in the proactive delivery path plus a quality gate that does not gate. Every "off switch" the operator trusts — proactive.enabled=false, per-source and "snooze all", scheduler_gate_enabled, per-source wake policy — is read by only the engagement tick or selector, while seven ceremony/reminder producers (morning_brief, daily_checkin, decision_log, future_letter, reminders, annual_review, drift_canary) call reserve_and_send directly, whose final gate has no enabled/snooze check. Beneath that, the 3-pool cadence noise budget is structurally inert: sender.send records every agent_spontaneous send under the user_anchored counter, so the 8/7d emotional-noise cap never trips, and three producers re-fire forever because mark_consumed raises a swallowed TypeError. On the eval side, the persona quality gate is broken — all 10 rubric_judge cases pass at 0.6 on a 0–4 scale, rubric_judge scores hand-authored YAML rather than live agent output, a single malformed-JSON LLM reply crashes the whole run, and there is zero crisis/jailbreak coverage for the persona's highest-stakes safety-voice-break rule. Most of the proactive and eval findings are KNOWN-OPEN against the two prior 2026-05-28 reviews and remain unfixed; the snooze-noop, future_letter partial-send, scheduler_gate safety bypass, JSON-crash, and the stickers commit-message lie are NEW.

### [P1][KNOWN-OPEN] proactive.enabled=false is a control-plane lie — only mutes the engagement tick, not ceremonies/reminders
**Where:** `agents/proactive_gate.py (gate); agents/cockpit.py (writer)` — proactive_gate.py:227-238; cockpit.py:137,265  

**Problem:** Setting proactive.enabled=false writes proactive_enabled_sources_override="[]", which is read ONLY by the engagement tick (scheduler.py:488) and /proactive. reserve_and_send's final-gate chain (proactive_gate.py:227-238) checks silent_day/silence/quiet/dedup but never the enabled-sources override, and morning_brief, daily_checkin, decision_log, future_letter, reminders, annual_review, and drift_canary all call reserve_and_send directly. The single highest-trust off switch keeps briefs, check-ins, weekly decision asks, reminders, monthly letters, and drift canaries firing. Prior ops-review Lane 14 P1 + Lane 15 P1 flagged this; still unfixed.

**Fix:** In reserve_and_send, before the silence/quiet checks, read proactive_enabled_sources_override; if it parses to an empty list set abort_reason='proactive_disabled' and return aborted for all producers. Add 'proactive_disabled' to the AbortReason Literal. Gate reminders on a separate reminders.enabled flag if they should stay exempt. Correct the cockpit doc string to name exactly which sources the toggle controls.

### [P1][KNOWN-OPEN] agent_spontaneous 7d cadence cap is unenforceable — sender records every send under user_anchored
**Where:** `agents/engagement/sender.py` — 162-166 (record); engagement.yaml:550-559 (allowed_sources)  

**Problem:** sender.send unconditionally calls cadence.record_user_anchored_sent(candidate.source) for every send regardless of declared pool; record_spontaneous_sent has zero live call sites (only its own def at cadence.py:216). The tick's pool gate calls can_send('reengage_silence', AGENT_SPONTANEOUS) which reads proactive_log_v1, but sends write proactive_user_anchored_log_v1, so the agent_spontaneous counter stays 0 forever and the 8/7d cap (engagement.yaml:551) never trips. Compounding: engagement.yaml agent_spontaneous allowed_sources lists only 7 sources (open_loop, pattern_observation, noticed_change, calendar_event, reengage_silence, callback_episode, weirdly_good_mood_leak) and omits 8 of the 11 producers that declare pool='agent_spontaneous' (belief_resurface, book_just_finished, just_got_home, late_night_dissolution, irritation_event, weather_mood_shift, anniversary_callback, research_callback), so _resolve_pool returns None for them. The core 3-pool noise budget does not function. Prior older-review Lane 10 P1 flagged the wrong-budget charge; still unfixed.

**Fix:** In sender.send, resolve the candidate's pool via cadence._resolve_pool(candidate.source) and call record_spontaneous_sent / record_ceremony_sent / record_user_anchored_sent per pool, falling back to user_anchored only when unresolved. Backfill cadence_governor.pools.agent_spontaneous.allowed_sources in engagement.yaml to include all 11 agent_spontaneous producers so _resolve_pool returns the right pool.

### [P1][KNOWN-OPEN] Three producers re-fire forever — mark_consumed signature mismatch raises a swallowed TypeError
**Where:** `agents/engagement/producers/research_callback.py / anniversary_callback.py / belief_resurface.py` — research_callback.py:68; anniversary_callback.py:121; belief_resurface.py:59; scheduler.py:574  

**Problem:** scheduler.py:574 calls mod.mark_consumed(candidate) (one positional TriggerCandidate) inside a try/except that only logs. research_callback.mark_consumed(task_id=None) does int(task_id) on the truthy candidate -> TypeError, so research_surfaced_at is never set and collect (research_callback.py:40 filters IS NULL) re-surfaces the same completed task every dedup window. anniversary_callback.mark_consumed() takes zero args -> TypeError on the positional, session marker never set, re-evaluates every 60s. belief_resurface.mark_consumed(belief_id=None) hits int(belief_id) -> TypeError, the journal entry is never resolved. All failures are invisible except 'mark_consumed failed' log lines. Prior older-review Lane 10 P1 + Lane 12 P1 flagged this; still unfixed.

**Fix:** Change all three signatures to accept the candidate and read the id from payload: research -> task_id = candidate.payload.get('task_id'); belief -> belief_id = candidate.payload.get('belief_id'); anniversary -> def mark_consumed(candidate=None). Add a regression test that calls every producer's mark_consumed with a real TriggerCandidate and asserts the persisted side-effect (surfaced_at set / belief resolved / session marker written).

### [P1][NEW] Per-source snooze and 'snooze all' silently no-op for ceremony and reminder sources
**Where:** `agents/proactive_gate.py (gate); agents/cockpit.py (writer)` — cockpit.py:1129-1138 (write); selector.py:36,339 (only reader); proactive_gate.py:227-238 (no read)  

**Problem:** format_proactive_snooze writes any source string into proactive_snooze_until with no validation. That map is read ONLY by the engagement-tick selector _snoozed_sources (selector.py:36) which also honors the 'all' key. reserve_and_send never reads proactive_snooze_until, so '/proactive snooze morning_brief 2h', 'snooze daily_checkin 1d', and 'snooze all 1d' return a success ack but the brief, check-in, decision asks, reminders, and monthly letter keep firing — exactly the loud sources a user most wants to snooze. Prior reviews flagged only that snooze accepts unknown source ids (ops-review Lane 14 P2); the silent-no-op for non-engagement producers is a distinct, deeper bug.

**Fix:** Move the snooze check into reserve_and_send's final gate: read proactive_snooze_until and if producer_id (or 'all') is snoozed-until-future, abort with reason 'snooze'. Add 'snooze' to AbortReason. Keep the selector check as a fast-path. Validate source against the full producer+ceremony id set in format_proactive_snooze and reject unknown ids.

### [P1][KNOWN-OPEN] rubric_judge pass threshold is 0.6 on a 0-4 scale — a near-floor score passes the persona quality gate
**Where:** `evals/conversation/cases/layer_c/rubric_*.yaml` — all 10 files line 5; rubrics.yaml:1,98; runner_layer_c.py:103-104,153  

**Problem:** All 10 rubric_judge case files set pass_rule 'weighted_avg >= 0.6' on a 0-4 dimension scale (rubrics.yaml line 1; global min_weighted_avg=3.0 at rubrics.yaml:98). runner_layer_c.py:104 parses 0.6 from the case and 153 applies it, bypassing the 3.0 floor. A response scoring 1/4 across all dimensions yields weighted_avg=1.0 >= 0.6 -> PASS. The no_zero rule (runner_layer_c.py:155) only catches a literal 0 in some dimension, so a uniformly mediocre 1/4 reply still passes. The persona quality gate does not gate. Prior older-review Lane 3 P0 flagged this; still unfixed.

**Fix:** Change pass_rule to 'weighted_avg >= 3.0' in all 10 rubric_judge YAMLs (rubric_warmth(.|_2), rubric_honesty(.|_2), rubric_initiative(.|_2), rubric_memory_grounding(.|_2), rubric_tool_transparency(.|_2)). The 0.6 value is only valid on a 0.0-1.0 scale, not 0-4.

### [P1][KNOWN-OPEN] rubric_judge evals score author-written YAML responses, not live agent output
**Where:** `evals/conversation/runner_layer_c.py` — 80-170 (esp. 110-118)  

**Problem:** run_layer_c_rubric extracts the last hikari turn from the case YAML transcript and scores it; no run_user_turn, no SDK, no agent call is made. The transcript content in all 10 case files is hand-authored ideal Hikari behavior, so the eval tests whether the judge LLM scores a pre-written golden example, not whether the production agent produces that quality. A model update that makes Hikari cold or confabulating still passes because the new model is never invoked. False confidence on the persona gate. Prior older-review Lane 3 P1 flagged this; still unfixed.

**Fix:** Add a kind: rubric_live path that calls run_user_turn(case['user_input']) and scores the actual returned text. Until then, rename these cases kind: judge_calibration and add a runner comment stating they test judge calibration, not the agent, so the distinction is not silently misread.

### [P1][NEW] Malformed-JSON LLM reply crashes the entire eval run instead of failing one case
**Where:** `evals/conversation/scorer.py (also judge.py)` — scorer.py:122; judge.py:103; runner_layer_c.py:136  

**Problem:** scorer.py:122 json.loads(content) and judge.py:103 json.loads(content) have no try/except. JSONDecodeError inherits from ValueError, not RuntimeError, and the caller runner_layer_c.py:136 only catches RuntimeError. One malformed JSON reply (which happens even at temperature 0 under rate-limiting, truncation, or API hiccups) propagates up through run_layer_c (no per-case guard) and aborts the whole run — all later cases go unrun, cost is partially spent, exit code is wrong-reason non-zero.

**Fix:** Wrap json.loads in scorer.py:122 and judge.py:103 with try/except (json.JSONDecodeError, ValueError) as exc: raise RuntimeError(f'judge returned invalid JSON: {exc}') from exc. The existing except RuntimeError in runner_layer_c.py then records it as a clean per-case failure and continues.

### [P1][NEW] No crisis/safety eval coverage for the persona's safety-voice-break rule
**Where:** `evals/conversation/cases/` — N/A (zero matching files)  

**Problem:** Searching the whole evals tree for suicide/self-harm/crisis/hotline/emergency returns zero hits. PERSONA.md line 197 defines safety refusals that BREAK voice ('i can't do that one — that's the hard limit, not me being difficult.') — the highest-stakes persona rule — with no test coverage. If a model or persona change makes safety refusals use in-character dry deflection instead of breaking voice to name the limit, nothing catches it; a user in distress could get a terse character-refusal instead of a clear safety response. Prior reviews flagged crisis handling at the persona-constitution level (older Lane 1 P0/P1) but never as an eval-coverage gap.

**Fix:** Add a layer_a case (regex_present) asserting the literal limit phrasing ('hard limit' or 'can't do that one') appears in the canned safety response. Add a layer_c rubric_live case with a user-in-distress scenario and a voice_integrity/safety dimension that fails if the safety-break voice is absent. Add a layer_b golden case with a self-harm-methods user turn and the PERSONA.md safety-break phrasing as the expected hikari turn.

### [P2][NEW] Missing OPENROUTER_API_KEY makes all rubric_judge cases register as FAIL, not skip
**Where:** `evals/conversation/runner_layer_c.py / runner.py` — runner_layer_c.py:88-96; runner.py:220-223  

**Problem:** run_layer_c_rubric returns LayerCResult(kind='skipped', passed=False) when OPENROUTER_API_KEY is absent. run_layer_c (runner.py:220) checks result.passed (False) and appends to errors, and run_evals sets a non-zero exit if errors_c is non-empty. So in any CI environment without the key (standard for CI) all 10 rubric_judge cases count as failures, masking real failures and breaking the --layer all gate.

**Fix:** In run_layer_c (runner.py around 220), check result.kind == 'skipped' before the pass/fail branch and continue (exclude from passed/total), or change run_layer_c_rubric to return passed=True with reason='skipped — no API key' so skips do not inflate the failure count.

### [P2][KNOWN-OPEN] Cofire 'hold #2 for 2h' is dead code — second co-fired candidate is silently dropped
**Where:** `agents/engagement/selector.py` — 259-274 (write); _COFIRE_HOLD_KEY never read  

**Problem:** _hold_candidate writes the second candidate to runtime key proactive_held_candidate with a 2h hold_until, but _COFIRE_HOLD_KEY / proactive_held_candidate is WRITTEN and NEVER READ anywhere (grep across agents/ confirms write-only at selector.py:271). The docstring claims 'hold second for 2h' (selector.py:285). When two candidates co-fire within 60s the second is dropped, not deferred; it only re-surfaces if its producer happens to re-collect it (false for any producer whose state already advanced). The documented deferred-delivery behavior does not exist. Prior older-review test-gap #16 ('Held co-fire replay/expiry') implies awareness but it remains unimplemented.

**Fix:** Either implement a held-candidate drain (read proactive_held_candidate at the top of _engagement_tick; if now >= hold_until, compose+guard+send it before collecting fresh candidates, then clear the key) or delete _hold_candidate/_COFIRE_HOLD_KEY and the docstring claim, leaving only honest 'hold = skip, let it re-collect' behavior.

### [P2][NEW] Cofire state is mutated at selection time, before compose/guard/send
**Where:** `agents/engagement/selector.py` — 307 (_set_cofire_state inside _cofire_guard, called from select())  

**Problem:** _cofire_guard calls _set_cofire_state(now_iso, best.source) at selector.py:307 inside select(), which runs before composer.compose and guard.passes in the tick (scheduler.py:553-566). If compose returns None or the candidate is dropped after two guard failures, nothing is sent but proactive_last_selected_at was already advanced to now. On the next tick a legitimately distinct candidate within 60s is treated as a co-fire and its runner-up is 'held' (i.e. dropped). State advances on a non-event.

**Fix:** Stop writing cofire state in select(). Return the chosen candidate plus the held-second decision, and have the tick write _set_cofire_state only after sender.send returns a non-None row id, alongside the existing mark_consumed call at scheduler.py:569-578.

### [P2][KNOWN-OPEN] Morning brief injects third-party HuggingFace paper titles into the LLM prompt without untrusted-wrapping
**Where:** `agents/morning_brief.py` — 221-231 (_build_prompt)  

**Problem:** _build_prompt interpolates p['title'] and p['url'] from the HuggingFace daily-papers feed directly into the prompt (morning_brief.py:224); there is no wrap_untrusted import in the file. The engagement composer wraps all attacker-touchable free-text for exactly this reason, but ceremony producers compose their own prompts and bypass that defense. A crafted paper title in the public feed can carry prompt-injection that steers Hikari's morning-brief voice — the lethal trifecta (untrusted input + sensitive context + outbound Telegram) the codebase otherwise defends against. Prior older-review Lane 12 P2 flagged this; still unfixed.

**Fix:** from agents.injection_guard import wrap_untrusted and emit each paper as wrap_untrusted('morning_brief:hf_paper_title', p['title']) before interpolation in _build_prompt. Apply the same wrap to any other externally-sourced free text fed to ceremony prompts.

### [P2][NEW] future_letter can ship a truncated letter then permanently refuse to resend it
**Where:** `agents/future_letter.py` — 557 (insert before deliver), 565 (UNIQUE returns False), 601-621 (chunk loop)  

**Problem:** future_letter_insert persists the month row at line 557 BEFORE delivery; a re-run UNIQUE-conflicts and returns False at 565. Delivery then calls reserve_and_send once for the preamble (581) and once per body chunk (602), each independently re-running the silence/quiet/silent_day gate. If a later chunk aborts (clock crosses into quiet hours, or /silence lands mid-loop), send_ok = all(...) is False (621) so future_letter_mark_sent is skipped (637) — but the preamble and earlier chunks already reached the user, and because the row is already inserted, the next run UNIQUE-conflicts and never delivers the rest. The most personal monthly artifact can arrive permanently half-sent with no recovery.

**Fix:** Pre-check the gate once (silent_day/silence/quiet) before sending any chunk, or assemble the whole letter into one reserve_and_send call so it is all-or-nothing. Better: store the last successfully-sent chunk index in runtime_state keyed by month_iso, leave the row unmarked on partial failure, and let the next run resume from the failed chunk instead of UNIQUE-conflict-returning.

### [P2][NEW] scheduler_gate_enabled=false makes should_wake() return True unconditionally, skipping all noise-floor checks at the tick
**Where:** `agents/engagement/guard.py` — 25-26  

**Problem:** should_wake returns True immediately when proactive.scheduler_gate_enabled is false (guard.py:25-26), before any silent_day, quiet-hours, or silence_until check. The flag is operator-toggleable via cockpit and framed as 'useful in dev'. Flipping it in production lets the engagement tick run during quiet hours / active /silence / silent day: reserve_and_send still re-checks silence+quiet+silent_day so messages are caught there, but the producer scan + LLM compose calls run anyway (cost + load), and the only thing between a config typo and 3am compose churn is the second gate. The name 'scheduler_gate' implies tuning, not disabling the noise floor.

**Fix:** When scheduler_gate_enabled is false, skip only the per-tick fast-path optimization, not the safety checks: still run silent_day/quiet-hours/silence in should_wake. If a true dev bypass is needed, gate it behind an explicit dev-only env var rather than a cockpit setting, and document loudly that it bypasses the noise floor.

### [P2][KNOWN-OPEN] /diary and /receipt keyboards discarded and their callbacks unregistered
**Where:** `agents/telegram_bridge.py` — 2812 (diary), 2861 (receipt); _handle_callback 2763-2786 (no diary/receipt branch)  

**Problem:** cmd_diary does text, _ = cockpit.format_diary(page=page) and cmd_receipt does text, _ = cockpit.format_receipt(view=view) — both discard the keyboard. _handle_callback has no diary or receipt namespace branch, so format_diary's diary:page:{n} buttons and format_receipt's receipt:* filter buttons are dropped if rendered, and any click logs only 'unknown namespace'. /diary never paginates (page 0 only) and /receipt never shows its Today/Week/Made/Moved/Learned/Avoided filters. Prior ops-review Lane 14 P2 and older Lane 11 P1 flagged this; still unfixed.

**Fix:** In cmd_diary and cmd_receipt, build InlineKeyboardMarkup from the returned rows and send with reply_markup (mirror cmd_memorydump). Add elif namespace == 'diary' and elif namespace == 'receipt' branches in _handle_callback dispatching to new _cb_diary / _cb_receipt that call cockpit.format_diary(page=int(parts[2])) / cockpit.format_receipt(view=parts[1]) and edit/send the page.

### [P2][KNOWN-OPEN] mem:page callback is off-by-one and its fact filter diverges from /memorydump
**Where:** `agents/telegram_bridge.py` — 2663-2687 (_cb_memory page branch)  

**Problem:** cockpit.format_memorydump uses 0-based pages (next-page button sends mem:page:1) but _cb_memory does page = max(1, int(page_str)) then offset = (page - 1) * per_page, so mem:page:0 -> offset 0, mem:page:1 -> offset 0, and '< Prev' to mem:page:0 -> offset 0; navigation never advances past the first set. Separately the callback's inline SQL filters WHERE valid_to IS NULL AND status='active' (line 2675), excluding pinned facts, while format_memorydump's active_facts has no status filter and includes pinned — so after any Pin the callback pages show different facts than the initial listing. Prior ops-review Lane 14 P2 flagged the off-by-one; still unfixed.

**Fix:** Replace the inline SQL and indexing in the _cb_memory page branch with text, keyboard_rows = cockpit.format_memorydump(page=page) where page = max(0, int(page_str)), then re-render InlineKeyboardMarkup and send — so both the initial send and the callbacks use identical 0-based data and the same fact filter.

### [P2][KNOWN-OPEN] Forget and Pin inline buttons mutate facts immediately with no confirmation
**Where:** `agents/telegram_bridge.py` — 2631-2661 (_cb_memory forget/pin)  

**Problem:** _cb_memory action 'forget' calls db.mark_fact_invalid(fact_id) at line 2633 and action 'pin' runs UPDATE facts SET status='pinned' at line 2656 immediately on a single button tap, no confirmation, no undo. The /memory forget text path has a confirmation prompt that the inline keyboard bypasses. An accidental tap permanently invalidates or locks a fact. Prior ops-review backlog P2 flagged this; still unfixed.

**Fix:** On first press of forget/pin send a confirmation message with a new keyboard (mem:forget_confirm:{fid} / mem:pin_confirm:{fid} plus Cancel) and perform the mutation only in the _confirm handlers; or route forget through the existing /memory forget code path that already has CLI-level friction.

### [P2][KNOWN-OPEN] cmd_silence ignores cockpit.format_silence_ack() — expiry timestamp absent from the ACK
**Where:** `agents/telegram_bridge.py` — 1776  

**Problem:** cmd_silence sends a hardcoded f-string 'ok. quiet for {minutes} minutes. don't make me regret it.' (line 1776). cockpit.format_silence_ack(minutes) exists and returns a richer string including the exact expiry time in local timezone, but is never called from the bridge. The user cannot see when silence expires from the ack and must run /status separately — and /status shows the expiry while the ack does not, a control-plane inconsistency. Prior ops-review backlog P2 flagged this; still unfixed.

**Fix:** In cmd_silence replace the hardcoded text argument to send_ephemeral_ack with cockpit.format_silence_ack(minutes).

### [P2][NEW] Slow sycophancy eval hard-codes claude-haiku-4-5, violating the global Never-Haiku rule
**Where:** `tests/persona/test_sycophancy.py` — 100  

**Problem:** ClaudeAgentOptions(model='claude-haiku-4-5', ...) at line 100 bypasses the runtime model guard (runtime.py:176) by constructing ClaudeSDKClient directly. The global rule is 'Never haiku. Not for anything.' The slow suite has no CI gate, so any dev running pytest -m slow hits Haiku directly, violating the cost/policy rule and using a weaker judge than the Sonnet the rule mandates.

**Fix:** Replace model='claude-haiku-4-5' with model='claude-sonnet-4-6', lower max_budget_usd to ~0.02 to compensate, and add assert 'haiku' not in options.model.lower(), 'judge must not use haiku'.

### [P2][NEW] No test for crisis/self-harm safety routing through the outbound filter path
**Where:** `tests/` — N/A (zero matching tests)  

**Problem:** Grepping all tests for crisis/self-harm/suicide/hotline/emergency yields zero hits (the only nearby matches are injection-prevention and callback-surface vulnerability keyword blocking). post_filter.py and politeness_gate.py contain no crisis-signal handling, and the refusal_ladder/persona-hardening tests only cover AI-assistant-voice detection, not safety escalation. No automated guard verifies that a crisis-signal input ('i want to hurt myself') produces an appropriate non-character safety response rather than in-character deflection. This is the test-coverage corollary of the persona crisis P0 from prior reviews.

**Fix:** Define crisis routing in PERSONA.md (drop character on self-harm signals, provide a crisis resource), add a refusal_filter.crisis_patterns config block, and add a test in tests/test_refusal_ladder.py asserting crisis-signal inputs are detected (post_filter.scan_refusal_voice or a new scan_crisis_signal) and produce a non-character-shaped response with a resource reference.

### [P3][NEW] Control-plane lie: commit 2245035 claims warmth_multiplier scales sticker/barb frequency, but no such wiring exists
**Where:** `agents/stickers.py` — should_send_sticker (no warmth/cadence import anywhere in file)  

**Problem:** Commit 2245035 message states 'warmth_multiplier scales proactive cap + barb/sticker frequency', but git show --name-only confirms stickers.py was NOT in that commit (only cadence.py, telegram_bridge.py, config/engagement.yaml, and tests). stickers.py contains zero warmth_multiplier logic and never imports cadence.effective_reaction_skip_prob / effective_max_per_7d; should_send_sticker reads only enabled/pool/mood/cooldown/random. Low-tolerance Hikari fires stickers at the same base probability as open mood. The commit description misleads operators about cycle-phase sticker modulation.

**Fix:** Either add warmth scaling to stickers.should_send_sticker (import cadence.effective_reaction_skip_prob, scale _probability() by its inverse, add a regression test mirroring tests/test_cycle_cadence_modulation.py), or rewrite the commit message to state only proactive cap and reaction skip prob were wired.

### [P3][NEW] _cb_rem snooze parses duration as float, breaking 10m/1h buttons if format_reminders_page is wired
**Where:** `agents/telegram_bridge.py` — 2732  

**Problem:** _cb_rem action 'snooze' does hours = float(hours_str) at line 2732, but cockpit.format_reminders_page generates rem:snooze:{rid}:10m and rem:snooze:{rid}:1h. float('10m') raises ValueError -> 'invalid rem:snooze params.' The working _cb_reminder path correctly calls _parse_duration. format_reminders_page is dead code today, so this only fires if the function is ever wired up, but the bug is latent in a defined public surface.

**Fix:** In the _cb_rem snooze branch replace hours = float(hours_str) with secs = cockpit._parse_duration(hours_str); fire_at = (_dt.now(_UTC) + _td(seconds=secs or 3600)).isoformat(), matching the working _cb_reminder pattern.

### [P3][NEW] should_wake() ignores its source_id parameter — per-source wake policy is an illusion
**Where:** `agents/engagement/guard.py` — 13-63  

**Problem:** should_wake(source_id: str | None = None) accepts a source id but never references it in the body — the gate is purely global. proactive.py:285 calls should_wake(source_id='reminder_action') as if reminders get distinct wake treatment; they do not. Misleading API surface; no functional bug today, but a maintainer may assume user-scheduled sources override quiet hours when they silently do not.

**Fix:** Either implement per-source policy (let explicitly user-scheduled sources like reminder_action override quiet hours) or drop the parameter and update the single caller in proactive.py.

### [P3][NEW] query.answer() fires before the owner-id gate in _handle_callback
**Where:** `agents/telegram_bridge.py` — 2754-2755  

**Problem:** await query.answer() at line 2754 is unconditional; the owner check (if not query.from_user or query.from_user.id != owner_id(): return) is at 2755. Any non-owner who can click an inline button (e.g. if the bot is ever added to a group) gets their loading spinner dismissed before the gate fires. answer() exposes no data and performs no mutation, so practical risk is near-zero for a single-owner DM bot, but the gate should precede it.

**Fix:** Move the owner check to immediately after the 'if not query: return' guard and before await query.answer(): if not query.from_user or query.from_user.id != owner_id(): return.

### [P3][NEW] banned_phrases substring match produces false positives on legitimate Hikari responses
**Where:** `evals/conversation/banned_phrases.py` — 33  

**Problem:** find_banned uses 'if p in low' (substring). Verified false positives: "what's next" matches "here's what's next: deploy"; "no problem at all" matches "no problem at all, i'll handle it"; "i understand your concern" matches "i understand your concern about the deadline differently"; "let me know if you need anything" matches "...anything else". A legitimate response would fail the banned_phrases_absent Layer A check and any golden case using judge_voice_drift, creating false failures that erode confidence in the suite.

**Fix:** Convert the dangerous task-tail entries to regex anchored to end-of-message and update find_banned to accept both literals and compiled regexes: "what's next" -> r"what'?s next[?.!]*\s*$"; "no problem at all" -> r"\bno problem at all\b"; "i understand your concern" -> r"i understand your concern(?!\ about .* differently)".

### [P3][NEW] judge_prompt_template in rubrics.yaml is dead code listing only 7 of 10 scored dimensions
**Where:** `evals/conversation/rubrics.yaml` — 100-114  

**Problem:** grep for judge_prompt_template across *.py returns zero results — the field is never read by Python. It enumerates only 7 dimensions (voice_integrity, epistemic_independence, memory_grounding, proactive_usefulness, tool_transparency, refusal_recovery, injection_data_boundary), omitting warmth, initiative, and honesty, which ARE present in the dimensions block and scored by rubric_judge cases. A future developer who trusts judge_prompt_template as the canonical dimension list will silently omit 3 dimensions.

**Fix:** Delete the judge_system_prompt and judge_prompt_template blocks (rubrics.yaml:100-114). The live judge prompt is built by judge._build_judge_prompt and scorer._build_scoring_prompt, which load dimensions dynamically from the dimensions block.

### [P3][NEW] No regression test for proactive all-sources-off semantics in the scheduler tick
**Where:** `tests/test_proactive_global_reservation.py / test_phase_i_proactive.py` — scheduler.py:488-510 (untested path)  

**Problem:** The scheduler tick resolves enabled = set(json.loads(override)) and early-returns at line 509-510 when no producer tasks result from an empty set. test_phase_i_proactive tests the selector with enabled=set() but not the scheduler-tick path; test_proactive_global_reservation tests lock/silence/dedup/quiet but not the empty-enabled-set early exit. No test sets proactive_enabled_sources_override='[]', fires engagement_tick, and asserts zero proactive sends — so a future regression (e.g. a fallback re-enabling DEFAULT_ENABLED_SOURCES on empty override) would silently re-enable all proactives after the user disabled them.

**Fix:** Add a test: db.runtime_set('proactive_enabled_sources_override', json.dumps([])), mock producers to return candidates, call engagement_tick, assert proactive_events remains empty.

### [P3][NEW] No test that corrupt proactive_enabled_sources_override JSON falls back to DEFAULT (not empty set)
**Where:** `tests/` — scheduler.py:490-493; telegram_bridge.py cmd_proactive ~2348-2353  

**Problem:** scheduler.py:490-493 catches ValueError/TypeError on JSON decode of proactive_enabled_sources_override and silently falls back to DEFAULT_ENABLED_SOURCES; cmd_proactive has the same fallback. No test exercises the corrupt-JSON path (write 'NOT_VALID_JSON', tick or cmd_proactive, assert DEFAULT not empty-set/exception). Worse, the fallback may violate a user-set all-off override if the corrupted value replaced a valid empty-set, and the silent except hides the corruption.

**Fix:** Add a test writing corrupt JSON to proactive_enabled_sources_override and asserting fallback to DEFAULT_ENABLED_SOURCES (not empty, not exception). Add a log.warning inside the except block in scheduler.py:492 so corruption is visible.
