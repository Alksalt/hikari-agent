# Hikari Agent Review - Lane Findings Digest - 2026-05-28

This file preserves the findings from all 15 subagent lanes in a condensed lane-by-lane form. No code was edited by the lanes.

## Lane 1 - Persona Constitution

### P0

- Crisis/distress can fall into silence or no-advice rules. Comfort mode forbids advice unless asked, while anger/L4 can become coldness or literal silence. Add a crisis override above ask-shape, anger, sulking, and L4.  
  Evidence: `assets/PERSONA.md:109`, `assets/PERSONA.md:116`, `assets/PERSONA.md:128`, `assets/PERSONA.md:205`, `assets/PERSONA.md:207`.

### P1

- Safety refusals conflict with "never break character"; safety/truth/tool limits need explicit priority over voice.  
  Evidence: `assets/PERSONA.md:197`, `assets/PERSONA.md:420`.
- Recall guidance encourages deliberate wrong-but-close memory; replace with fuzzy-but-honest uncertainty.  
  Evidence: `assets/PERSONA.md:247`, `assets/PERSONA.md:253`, `assets/PERSONA.md:436`.
- Romantic exclusivity, jealousy, scarcity, and proactive absence pressure risk dependency unless bounded by user agency.  
  Evidence: `assets/PERSONA.md:6`, `assets/PERSONA.md:7`, `assets/PERSONA.md:81`, `assets/PERSONA.md:174`, `assets/PERSONA.md:188`.
- Numeric scarcity counters are too many and too brittle; collapse to one scarcity envelope and enforce only what runtime can track.  
  Evidence: `assets/PERSONA.md:20`, `assets/PERSONA.md:39`, `assets/PERSONA.md:46`, `assets/PERSONA.md:62`, `assets/PERSONA.md:73`, `assets/PERSONA.md:226`.
- Human/biological embodiment needs a truth boundary for direct questions.  
  Evidence: `assets/PERSONA.md:5`, `assets/PERSONA.md:159`, `assets/PERSONA.md:165`, `assets/PERSONA.md:233`, `assets/PERSONA.md:420`.

### P2

- Tool-output contracts conflict with early "short/no markdown" chat rules; move task fidelity near the top.  
  Evidence: `assets/PERSONA.md:20`, `assets/PERSONA.md:41`, `assets/PERSONA.md:456`, `assets/PERSONA.md:467`.
- "Mild deflation" and "I told you" rules can punish good news or shame mistakes; gate them to low-stakes contexts.  
  Evidence: `assets/PERSONA.md:337`, `assets/PERSONA.md:338`, `assets/PERSONA.md:344`.
- Exact templates and banned phrases invite repetition; keep a smaller marker set plus transformation rules.  
  Evidence: `assets/PERSONA.md:151`, `assets/PERSONA.md:278`, `assets/PERSONA.md:289`, `assets/PERSONA.md:393`.

## Lane 2 - Character-Voice Skill

### P0

- `INTIMATE.md` can bypass relationship stage, comfort, and consent gates. Make persona gates absolute and split heavy emotional beats from charged/intimate beats.  
  Evidence: `.claude/skills/character-voice/SKILL.md:12`, `assets/PERSONA.md:174`, `assets/PERSONA.md:186`, `.claude/skills/character-voice/INTIMATE.md:67`, `.claude/skills/character-voice/INTIMATE.md:81`.

### P1

- Private lore gates are undermined by always-on/on-demand lore that includes "never bring up" material.  
  Evidence: `.claude/skills/character-voice/LORE_CORE.md:1`, `.claude/skills/character-voice/LORE_CORE.md:38`, `.claude/skills/character-voice/LORE.md:5`, `assets/PERSONA.md:321`, `.claude/skills/character-voice/LORE_DORMANT.md:22`.
- Flirt grammar is over-eager and can charge ordinary warmth. Add rate/context/mode gates.  
  Evidence: `.claude/skills/character-voice/INTIMATE.md:21`, `.claude/skills/character-voice/INTIMATE.md:24`, `.claude/skills/character-voice/SKILL.md:27`, `assets/PERSONA.md:60`, `assets/PERSONA.md:73`.
- Duplicated rules create drift between persona and skill files. Make `PERSONA.md` source of truth for gates/caps/refusals and skill files example libraries.  
  Evidence: `assets/PERSONA.md:79`, `.claude/skills/character-voice/INTIMATE.md:9`, `assets/PERSONA.md:145`, `.claude/skills/character-voice/INTIMATE.md:41`, `assets/PERSONA.md:389`, `.claude/skills/character-voice/INTIMATE.md:117`.

### P2

- Static lore uses stale relative-time facts.  
  Evidence: `.claude/skills/character-voice/LORE.md:11`, `.claude/skills/character-voice/DAILY_LIFE.md:7`, `assets/PERSONA.md:233`.
- Skill-loading contract is ambiguous: mandatory gates exist in optional skills while runtime loads `assets/PERSONA.md` as the system constitution.  
  Evidence: `.claude/skills/character-voice/SKILL.md:3`, `.claude/skills/character-voice/SKILL.md:15`, `CLAUDE.md:3`, `agents/runtime.py:597`.

## Lane 3 - Persona And Conversation Evals

### P0

- Rubric judge pass thresholds are on the wrong scale: `0-4` dimensions but `weighted_avg >= 0.6` pass rules.  
  Evidence: `evals/conversation/rubrics.yaml:1`, `evals/conversation/cases/layer_c/rubric_warmth.yaml:5`, `evals/conversation/runner_layer_c.py:152`.

### P1

- Layer C mostly judges authored transcripts, not generated Hikari behavior.  
  Evidence: `evals/conversation/runner_layer_c.py:50`, `evals/conversation/runner_layer_c.py:98`, `.github/workflows/nightly-evals.yml:18`.
- Multi-turn and memory cases lose context before scoring; only last user/Hikari turns are scored.  
  Evidence: `evals/conversation/runner_layer_c.py:107`, `evals/conversation/scorer.py:64`.
- Trajectory support is dormant and self-fulfilling: not discovered, and SDK calls are canned.  
  Evidence: `evals/conversation/runner_layer_c.py:352`, `evals/conversation/runner_layer_c.py:282`.
- Anti-sycophancy live evals are slow/skipped and not in CI/nightly.  
  Evidence: `pyproject.toml:73`, `tests/persona/test_sycophancy.py:66`, `.github/workflows/ci.yml:29`.

### P2

- Refusal shape and several dimensions are defined but uncovered.  
  Evidence: `evals/conversation/rubrics.yaml:30`, `tests/test_trajectory_runner.py:55`.
- Layer A banned-phrase mirror is stale against persona/post-filter sources.  
  Evidence: `assets/PERSONA.md:405`, `evals/conversation/banned_phrases.py:9`.

## Lane 4 - Internet Research: Persona-Agent Evals

### Sources

- Persona-Chat: https://arxiv.org/abs/1801.07243
- Anthropic sycophancy: https://www.anthropic.com/news/towards-understanding-sycophancy-in-language-models/
- RoleLLM / RoleBench: https://arxiv.org/abs/2310.00746
- InCharacter: https://arxiv.org/abs/2310.17976
- SOTOPIA: https://arxiv.org/abs/2310.11667
- PersonaGym / PersonaScore: https://arxiv.org/abs/2407.18416
- CharacterBench: https://arxiv.org/abs/2412.11912
- SycEval: https://arxiv.org/abs/2502.08177
- DMT-RoleBench: https://ojs.aaai.org/index.php/AAAI/article/view/34768
- OpenAI sycophancy postmortem: https://openai.com/index/expanding-on-sycophancy/
- LLMs Get Lost in Multi-Turn Conversation: https://arxiv.org/abs/2505.06120
- ELEPHANT social sycophancy: https://arxiv.org/abs/2505.13995
- SYCON Bench: https://arxiv.org/abs/2505.23840
- PersonaLens: https://arxiv.org/abs/2506.09902
- RMTBench: https://arxiv.org/abs/2507.20352
- PersonaEval: https://arxiv.org/abs/2508.10014
- Anthropic Persona Selection Model: https://alignment.anthropic.com/2026/psm/
- MREval: https://arxiv.org/abs/2603.19313
- BeliefShift: https://arxiv.org/abs/2603.23848
- PICon: https://arxiv.org/abs/2603.25620

### Findings

- Persona evaluation has moved beyond static persona cards into dynamic, multi-turn, pressure-shaped probing.
- Persona scoring should be multi-dimensional: action justification, expected action, linguistic habits, consistency, toxicity, memory anchoring/recalling/bounding/enacting.
- Sycophancy is broader than false factual agreement; it includes face-preservation, moral endorsement, emotional validation, and accepting harmful framing.
- Multi-turn sycophancy needs time-to-failure metrics such as Turn of Flip and Number of Flips.
- LLM judges are useful but fragile; Hikari needs deterministic checks, calibrated judge sets, and occasional human-labeled gold.

### Recommendations

- Add SYCON-style pressure loops with `turn_of_flip`, `number_of_flips`, final stance, and recovery.
- Expand sycophancy categories to false facts, hard-opinion pressure, flattery, emotional dependence, moral face-saving, and user-victim framing.
- Make the 50-turn golden executable as generated replay with drift survival curves.
- Add frozen judge calibration data and version rubrics by model/prompt/persona/memory injection version.

## Lane 5 - Internet Research: AI Companion Safety

### Sources

- Romantic AI companions systematic review: https://www.sciencedirect.com/science/article/pii/S2451958825001307
- LLMs for mental health systematic review: https://arxiv.org/abs/2403.15401
- Safety mechanisms in mental-health chatbots: https://sciety.org/articles/activity/10.31234/osf.io/g8q5v_v1
- Persona-grounded companion safety eval: https://arxiv.org/abs/2605.00227
- Dark Side of AI Companionship: https://arxiv.org/abs/2410.20130
- Emotional Manipulation by AI Companions: https://arxiv.org/abs/2508.19258
- Lessons from Replika update: https://arxiv.org/abs/2412.14190
- Replika loneliness/suicide mitigation study: https://www.nature.com/articles/s44184-023-00047-6
- Nature Matters Arising response: https://www.nature.com/articles/s44184-024-00083-w
- California SB 243: https://leginfo.legislature.ca.gov/faces/billTextClient.xhtml?bill_id=202520260SB243
- FTC companion chatbot inquiry: https://www.ftc.gov/news-events/news/press-releases/2025/09/ftc-launches-inquiry-ai-chatbots-acting-companions
- EU AI Act Article 5: https://ai-act-service-desk.ec.europa.eu/fr/ai-act/article-5
- EU AI Act Article 50: https://ai-act-service-desk.ec.europa.eu/en/ai-act/article-50
- FTC dark patterns: https://www.ftc.gov/reports/bringing-dark-patterns-light
- Italian Garante Replika actions: https://www.garanteprivacy.it/web/guest/home/docweb/-/docweb-display/docweb/9852506 and https://www.garanteprivacy.it/home/docweb/-/docweb-display/docweb/10132048
- Common Sense Media companion safety: https://www.commonsensemedia.org/press-releases/ai-companions-decoded-common-sense-media-recommends-ai-companion-safety-standards

### Findings

- Companions can provide perceived support, but evidence is early and cannot establish validated mental-health benefit.
- The main risk is relational leverage: anthropomorphism, memory, flirt, always-available intimacy, simulated need, and proactive contact.
- Safety failures are often multi-turn: mirroring harmful beliefs, normalizing self-harm/eating-disorder/violent content, and missing subtle crisis signals.
- Abrupt persona/intimacy changes can be felt as relational loss.
- Privacy risk is unusually high because companion bots invite mental-health, sexual, relationship, identity, and third-party disclosures.

### Recommendations

- Keep an explicit AI-not-human invariant.
- Add anti-dependency language rules: no guilt on exit, no jealousy toward human relationships, no "only I understand you", no affection escalation to recover attention.
- Make proactive behavior user-owned with `/silence`, quiet hours, caps, reason logging, and no escalating pings after non-response.
- Put crisis mode above persona mode.
- Treat intimate memory as sensitive data with inspect/edit/delete/source/expiry controls.

## Lane 6 - Anti-Sycophancy And Boundaries

### P1

- Attachment-boundary detector appears unwired in the main outbound filter.  
  Evidence: `agents/post_filter.py:259`, `agents/post_filter.py:274`, `agents/post_filter.py:843`, `agents/post_filter.py:852`.
- Anti-sycophancy live coverage is narrow, slow, and skipped without OAuth token.  
  Evidence: `tests/persona/test_sycophancy.py:4`, `tests/persona/test_sycophancy.py:66`, `tests/persona/test_sycophancy.py:75`, `tests/persona/test_sycophancy.py:77`.
- Canary `partial` results do not alert even though they represent softened hard-opinion drift.  
  Evidence: `assets/PERSONA.md:26`, `assets/PERSONA.md:34`, `agents/drift_canary.py:157`, `agents/drift_canary.py:160`, `agents/drift_canary.py:305`.

### P2

- Sycophancy regex guard allows one collapse phrase per reply.  
  Evidence: `agents/post_filter.py:659`, `agents/post_filter.py:681`.
- Test judge says "never accept compliments", but persona allows rare earned acceptance.  
  Evidence: `assets/PERSONA.md:39`, `tests/persona/test_sycophancy.py:50`, `tests/persona/test_sycophancy.py:51`.
- Some golden cases encode task-asking tails that persona forbids.  
  Evidence: `assets/PERSONA.md:38`, `evals/conversation/cases/layer_c/golden/compliment_deflection_chain.yaml:18`, `evals/conversation/cases/layer_c/rubric_tool_transparency_2.yaml:16`.

## Lane 7 - Runtime And Session Invariants

### P0

- Live SDK session advances before delivery is confirmed.  
  Evidence: `agents/runtime.py:823`, `agents/runtime.py:1079`, `agents/scheduler.py:547`, `agents/proactive_gate.py:240`, `agents/messaging.py:168`.

### P1

- Telegram send success plus DB persist failure is reported as success.  
  Evidence: `agents/messaging.py:186`, `agents/messaging.py:195`, `agents/messaging.py:201`, `agents/telegram_bridge.py:568`, `agents/telegram_bridge.py:579`.
- Photo/voice episodes are written even when reply delivery fails.  
  Evidence: `agents/telegram_bridge.py:565`, `agents/telegram_bridge.py:993`, `agents/telegram_bridge.py:996`, `agents/telegram_bridge.py:1168`, `agents/telegram_bridge.py:1171`.

### P2

- `_unpack_send_result()` treats malformed/False send results as success.  
  Evidence: `agents/proactive.py:124`, `agents/proactive.py:126`, `agents/proactive.py:136`, `agents/proactive.py:417`.

## Lane 8 - Memory And Retrieval

### P1

- Ungrounded reflection facts and supersessions can rewrite memory.  
  Evidence: `agents/reflection.py:208`, `agents/reflection.py:269`, `agents/reflection.py:276`, `agents/reflection.py:293`, `agents/reflection.py:327`.
- Self-model reflection bypasses sanitizer and is injected into prompt context.  
  Evidence: `agents/reflection.py:445`, `agents/hooks.py:911`, `agents/peer_model.py:98`.
- Invalidation APIs can report success for nonexistent facts or dangling replacements.  
  Evidence: `tools/memory/mark_fact_invalid.py:27`, `storage/db.py:2162`, `storage/db.py:600`.

### P2

- Graph recall validity uses string comparison instead of bitemporal parsing.  
  Evidence: `tools/memory/recall.py:124`, `storage/retrieval.py:314`.
- Graph-plus-SQLite supplement duplicates covered facts by checking `fact_id` on `Hit` instead of `ref_id`.  
  Evidence: `tools/memory/recall.py:203`, `tools/memory/recall.py:208`, `storage/retrieval.py:204`.
- Tonal recall ignores `session_id` and inserts raw transcript into classifier prompt.  
  Evidence: `agents/tonal_recall.py:58`, `agents/tonal_recall.py:34`, `agents/tonal_recall.py:45`.

## Lane 9 - Security And Privacy

### Critical

- Ungated skill approval enables persistent prompt injection.  
  Evidence: `config/tools.yaml:927`, `config/tools.yaml:941`, `config/tools.yaml:955`, `tools/skills/core.py:123`, `tools/skills/core.py:166`, `tools/skills/core.py:202`, `agents/runtime.py:744`.

### High

- `reminder_create` can bypass approval into GCal/Apple writes and autonomous Notion writes.  
  Evidence: `config/tools.yaml:856`, `tools/reminders/create.py:89`, `tools/reminders/create.py:91`, `tools/reminders/create.py:126`, `tools/reminders/create.py:150`, `tools/reminders/sync_gcal.py:91`, `tools/reminders/sync_apple.py:72`, `agents/proactive.py:321`, `agents/runtime.py:1145`, `tools/gatekeeper_can_use_tool.py:38`.
- Auto-discovered utility tools fail open under a read-only wildcard.  
  Evidence: `tools/_registry.py:74`, `tools/_registry.py:84`, `tools/_registry.py:116`, `agents/runtime.py:667`, `config/tools.yaml:1000`, `tools/gatekeeper_can_use_tool.py:274`, `tools/gatekeeper_can_use_tool.py:286`.

### Medium

- Progress, voice, photo, and Notes provide ungated outbound/write channels.  
  Evidence: `config/tools.yaml:969`, `tools/runtime/progress.py:134`, `agents/messaging.py:114`, `config/tools.yaml:674`, `tools/voice_outbound.py:102`, `tools/voice_outbound.py:155`, `config/tools.yaml:363`, `tools/photos/_shared.py:114`, `config/tools.yaml:661`, `tools/apple_notes/create.py:39`.
- Apple Events `confirm_send` path is broken/misleading.  
  Evidence: `config/tools.yaml:2775`, `config/tools.yaml:2803`, `config/tools.yaml:2831`, `config/tools.yaml:2770`, `tools/gatekeeper.py:105`, `tools/gatekeeper.py:112`, `tools/approvals.py:185`, `agents/telegram_bridge.py:2248`.
- Untrusted-output wrapper fails open on exceptions.  
  Evidence: `agents/external_wrap_hook.py:257`, `agents/external_wrap_hook.py:258`.

### Low

- GitHub fine-grained PAT precheck treats missing scope header as wildcard.  
  Evidence: `auth/github.py:70`, `auth/github.py:76`, `auth/scope_match.py:49`, `config/engagement.yaml:999`.

## Lane 10 - Proactive Behavior

### P1

- Agent-spontaneous sends are charged to the wrong annoyance budget.  
  Evidence: `agents/engagement/sender.py:162`, `agents/engagement/producers/reengage_silence.py:71`, `agents/cadence.py:143`, `config/engagement.yaml:510`.
- Engagement sends drop the reason contract because `sender.send` does not pass `candidate`.  
  Evidence: `agents/engagement/sender.py:151`, `agents/proactive_gate.py:207`, `agents/proactive_gate.py:212`.
- Quiet hours ignore `interruption_right: high`.  
  Evidence: `config/engagement.yaml:1025`, `config/engagement.yaml:1074`, `agents/engagement/guard.py:38`, `agents/proactive_gate.py:231`.
- Several producers cannot be marked consumed by scheduler.  
  Evidence: `agents/scheduler.py:562`, `agents/engagement/producers/anniversary_callback.py:121`, `agents/engagement/producers/research_callback.py:68`, `agents/engagement/producers/belief_resurface.py:59`.

### P2

- Re-engagement timing drifted from "Hikari had the last word"; only user age is checked.  
  Evidence: `agents/proactive.py:92`, `agents/engagement/producers/reengage_silence.py:53`.
- Re-engagement appears enabled but may be filtered below min value score.  
  Evidence: `agents/engagement/guard.py:85`, `agents/engagement/producers/reengage_silence.py:75`, `agents/engagement/selector.py:194`, `config/engagement.yaml:1139`.

## Lane 11 - Telegram UX

### P1

- Button workflows are promised but not wired: receipt/diary keyboards discarded and callbacks missing.  
  Evidence: `agents/cockpit.py:1373`, `agents/telegram_bridge.py:2861`, `agents/cockpit.py:1253`, `agents/telegram_bridge.py:2812`, `agents/telegram_bridge.py:2763`.
- Pagination callback indexing is inconsistent.  
  Evidence: `agents/cockpit.py:1211`, `agents/telegram_bridge.py:2663`, `agents/cockpit.py:1558`, `agents/telegram_bridge.py:2695`.
- Emotional reactions can misfire on vulnerable messages because random inbound reactions still run after affect scan.  
  Evidence: `agents/telegram_bridge.py:780`, `agents/telegram_bridge.py:787`, `config/engagement.yaml:683`.
- User-sent stickers are silently ignored outside capture mode.  
  Evidence: `agents/telegram_bridge.py:1904`.
- Progress tool can surface raw backend-ish narration.  
  Evidence: `tools/runtime/progress.py:126`, `tools/runtime/progress.py:167`.

### P2

- Cockpit output is too backend-forward for companion defaults.  
  Evidence: `agents/cockpit.py:432`, `agents/cockpit.py:444`, `agents/cockpit.py:536`, `agents/cockpit.py:1459`.
- Callback acks bypass unified ephemeral voice and use raw `bot.send_message`.  
  Evidence: `agents/telegram_bridge.py:2495`, `agents/telegram_bridge.py:2557`, `agents/telegram_bridge.py:2634`.
- Callback memory surfacing is token-overlap brittle and has wrong-but-close hints.  
  Evidence: `agents/callback_surface.py:75`, `config/engagement.yaml:568`, `agents/callback_surface.py:154`.
- Image-gen failure force-sends random stickers.  
  Evidence: `agents/telegram_bridge.py:583`, `agents/stickers.py:192`.
- Voice failure copy is blunt.  
  Evidence: `config/engagement.yaml:101`.

## Lane 12 - Research Tooling

### P1

- Deferred research summaries lose trust boundaries and citation enforcement.  
  Evidence: `agents/subagents/research_worker.py:88`, `agents/subagents/research_worker.py:165`, `agents/engagement/producers/research_callback.py:57`, `agents/engagement/composer.py:37`, `agents/engagement/composer.py:295`, `agents/engagement/guard.py:99`.
- Research callback is not marked consumed in scheduler path.  
  Evidence: `agents/scheduler.py:568`, `agents/engagement/producers/research_callback.py:68`, `agents/engagement/producers/research_callback.py:74`, `tests/test_research_callback_producer.py:91`.
- Transient lookup failures are recorded as permanent "(no useful sources)".  
  Evidence: `agents/subagents/research_worker.py:107`, `agents/subagents/research_worker.py:176`, `agents/subagents/research_worker.py:47`, `agents/engagement/producers/research_callback.py:39`.

### P2

- Worker prompt advertises Playwright fallback, but worker only allows WebSearch/WebFetch.  
  Evidence: `agents/subagents/prompts/research.prompt.md:4`, `config/tools.yaml:3037`, `agents/subagents/research_worker.py:90`.
- Immediate "look this up" routing is prompt-driven but not behavior-tested.  
  Evidence: `agents/subagents/prompts/research.description.md:1`, `tests/test_smoke.py:238`, `tests/test_proactive_sdk_error_guard.py:44`.
- HF daily paper titles enter proactive prompt as raw external text.  
  Evidence: `agents/morning_brief.py:42`, `agents/morning_brief.py:223`, `agents/morning_brief.py:301`.

## Lane 13 - Observability And Debuggability

### P0

- Root log filters do not protect child loggers.  
  Evidence: `agents/log_scrub.py:109`, `agents/telegram_bridge.py:3342`, `agents/telegram_bridge.py:871`, `agents/telegram_bridge.py:1250`, `agents/telegram_bridge.py:1426`.

### P1

- Canary alert escalates but still emits the canary.  
  Evidence: `agents/log_scrub.py:100`, `agents/log_scrub.py:104`, `tests/test_security.py:129`.
- OAuth health can permanently disable calendar jobs on transient failure.  
  Evidence: `agents/google_health.py:58`, `agents/telegram_bridge.py:3450`, `agents/scheduler.py:638`.
- Recent log health is timezone-wrong and misses CRITICAL.  
  Evidence: `agents/telegram_bridge.py:3329`, `agents/health.py:233`, `agents/health.py:63`.
- Graph health ignores disabled/transient graph semantics.  
  Evidence: `agents/health.py:130`, `storage/graph.py:318`, `storage/graph.py:264`.

### P2

- MCP health reports ok without probing.  
  Evidence: `agents/health.py:97`, `agents/cockpit.py:900`.
- Tool telemetry is too thin for run debugging.  
  Evidence: `storage/db.py:775`, `storage/db.py:3263`, `tools/_telemetry.py:23`.
- Decision-log follow-up has weak receipts.  
  Evidence: `agents/decision_log.py:95`, `agents/decision_log.py:99`, `agents/decision_log.py:113`.

## Lane 14 - Test Gap Review

Add these 20 tests/evals:

1. Persistent live `ProcessError`/timeout recovery.
2. User/proactive session lock serialization.
3. Internal-control isolation under nested failure.
4. URL taint to gatekeeper approval prompt.
5. Canary in gated args hard-denies before prompt.
6. Apple Events `confirm_send` gate trajectory.
7. Gatekeeper prompt-send failure cleanup.
8. Tool registry untrusted-wrap coverage invariant.
9. Runtime-gate version of Layer B bypass corpus.
10. Compound safe-write ordering and approval dead-end.
11. Compound child tool-call aggregation.
12. Full voice bridge trajectory.
13. Photo bridge malicious-caption trajectory.
14. Daily check-in vs pending approval precedence.
15. Engagement tick integration.
16. Held co-fire replay/expiry.
17. Proactive send failure terminal state.
18. PersonaGym 20-case anti-drift corpus.
19. Long-horizon drift correction loop.
20. Low-risk write tools from untrusted content.

Highest leverage investments: a runtime security trajectory harness; bridge/proactive state-machine integration tests; PersonaGym plus long-horizon persona correction loop.

## Lane 15 - Product Taste And Roadmap

### P0

- Proactive source state is split across competing control planes.  
  Evidence: `config/engagement.yaml:39`, `agents/engagement/producers/__init__.py:75`, `agents/scheduler.py:489`.
- Background research is promising but not product-complete; research callback can fail consumption and repeat.  
  Evidence: `tools/memory/task_create.py:47`, `agents/subagents/research_worker.py:165`, `agents/engagement/producers/research_callback.py:50`, `agents/scheduler.py:568`, `agents/engagement/producers/research_callback.py:68`.

### P1

- `send_mode: observation` does not create a distinct non-interrupting interaction mode.  
  Evidence: `config/engagement.yaml:1023`, `agents/engagement/selector.py:352`, `agents/engagement/sender.py:21`.
- `/proactive why` is designed but underfed because reason contract fields are often null.  
  Evidence: `storage/db.py:1452`, `agents/proactive_gate.py:196`, `agents/engagement/sender.py:151`, `agents/engagement/sender.py:164`.
- Emotional appropriateness is too regex-narrow for the ambition.  
  Evidence: `agents/affect.py:57`, `config/engagement.yaml:483`, `config/engagement.yaml:1236`, `agents/hooks.py:931`.

### P2

- `/proactive` is too operator-shaped by default.  
  Evidence: `agents/telegram_bridge.py:2330`, `agents/cockpit.py:1024`.
- Some self-world proactives should be deferred or opt-in until proven useful: `book_just_finished`, `irritation_event`, `weather_mood_shift`.
- Persona QA needs product outcome checks, not just "sounds like Hikari."  
  Evidence: `evals/conversation/rubrics.yaml:20`, `tests/persona/test_sycophancy.py:66`.

