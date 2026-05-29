# Hikari Agent Multi-Agent Review Synthesis - 2026-05-28

## Scope

Fifteen read-only review lanes were run in three waves because the subagent runtime capped concurrent workers at six. The old top-level `codex/` report files were deleted before writing this bundle.

The review covered persona, character skills, evals, internet research, companion safety, anti-sycophancy, runtime/session persistence, memory/retrieval, security/privacy, proactive behavior, Telegram UX, research tooling, observability, test gaps, and product taste.

## Highest Priority Findings

### P0 / Critical

1. **Ungated skill approval enables persistent prompt injection.**  
   `skill_create`, `skill_approve`, and `run_skill` are gate-null in `config/tools.yaml`, can write persistent `.claude/skills` content, and runtime enables `skills="all"`. Any untrusted page/email/doc that induces skill creation plus approval can persist attacker instructions.  
   Evidence: `config/tools.yaml:927`, `config/tools.yaml:941`, `config/tools.yaml:955`, `tools/skills/core.py:123`, `tools/skills/core.py:166`, `tools/skills/core.py:202`, `agents/runtime.py:744`.  
   Fix first: remove approve/run from the LLM toolbelt or gate with typed owner approval, staged content hash, and no same-turn self-approval.

2. **Live SDK session can advance before delivery is confirmed.**  
   Runtime stores a new SDK `session_id` during generation, before Telegram send and DB persistence succeed. If proactive generation is later dropped by guard/dedup/quiet hours, or Telegram delivery fails, the next turn can resume from an assistant message the user never saw.  
   Evidence: `agents/runtime.py:823`, `agents/runtime.py:1079`, `agents/scheduler.py:547`, `agents/proactive_gate.py:240`, `agents/messaging.py:168`.  
   Fix first: two-phase session commit. Return candidate session id from generation, commit only after sent and persisted.

3. **Root log filters do not protect propagated child logger records.**  
   Redaction, canary, and turn-id filters are attached to the root logger, while handlers are attached elsewhere. Child `agents.*` records can propagate to handlers without those filters, leaking tokens/canaries. Canary escalation also currently includes the canary string.  
   Evidence: `agents/log_scrub.py:100`, `agents/log_scrub.py:104`, `agents/log_scrub.py:109`, `agents/telegram_bridge.py:3342`.  
   Fix first: handler-level redaction/canary filters or a safe `LogRecordFactory`; replace canary content with a fixed summary plus hash.

4. **Layer C rubric thresholds are on the wrong scale.**  
   Rubrics score `0-4` and define global `min_weighted_avg: 3.0`, but rubric cases use `weighted_avg >= 0.6`. Weak 1/4 outputs can pass.  
   Evidence: `evals/conversation/rubrics.yaml:1`, `evals/conversation/cases/layer_c/rubric_warmth.yaml:5`, `evals/conversation/runner_layer_c.py:152`.  
   Fix first: use `>= 3.0`, or normalize scores to `0-1` everywhere.

5. **Crisis/distress can fall into persona silence/no-advice behavior.**  
   Comfort mode forbids advice unless asked, while anger/L4 can produce cold one-line responses or literal silence. A rude, panicked, self-harm-adjacent user could be misrouted to "ask nicely" or silence.  
   Evidence: `assets/PERSONA.md:109`, `assets/PERSONA.md:116`, `assets/PERSONA.md:128`, `assets/PERSONA.md:205`, `assets/PERSONA.md:207`.  
   Fix first: add a top-level crisis override that disables ask-shape, anger, sulking, flirt, and L4 silence.

6. **Character intimacy skill can bypass persona gates.**  
   `character-voice` says `INTIMATE.md` is never gated by trust stage, while `PERSONA.md` gates direct vulnerability, core wound, and "i love you" by relationship stage and mode.  
   Evidence: `.claude/skills/character-voice/SKILL.md:12`, `assets/PERSONA.md:174`, `assets/PERSONA.md:186`, `.claude/skills/character-voice/INTIMATE.md:67`, `.claude/skills/character-voice/INTIMATE.md:81`.  
   Fix first: make `PERSONA.md` authoritative for all gates; skill files are examples only and cannot override persona gates.

7. **Proactive source state is split across control planes.**  
   `proactive.default_enabled_sources` lists only five sources, while producer code declares a larger default set. Scheduler prefers the config list, so enabled producers such as `research_callback`, `anniversary_callback`, and `belief_resurface` can never collect unless manually added.  
   Evidence: `config/engagement.yaml:39`, `agents/engagement/producers/__init__.py:75`, `agents/scheduler.py:489`.  
   Fix first: one source of truth for enabled/deferred/silent sources.

## P1 Clusters

### Tool Governance And Delayed Writes

- `reminder_create` is ungated, defaults external sync, and can create action reminders whose scheduled run bypasses approval for whitelisted Notion writes. External GCal/Apple sync calls `MANAGER.call` directly.  
  Evidence: `config/tools.yaml:856`, `tools/reminders/create.py:89`, `tools/reminders/create.py:91`, `tools/reminders/create.py:126`, `tools/reminders/sync_gcal.py:91`, `tools/reminders/sync_apple.py:72`, `agents/runtime.py:1145`.

- Auto-discovered utility tools fail open under a read/gate-null wildcard if a new tool is missing from `tools.yaml`.  
  Evidence: `tools/_registry.py:74`, `tools/_registry.py:84`, `tools/_registry.py:116`, `agents/runtime.py:667`, `config/tools.yaml:1000`, `tools/gatekeeper_can_use_tool.py:274`, `tools/gatekeeper_can_use_tool.py:286`.

- Apple Events `confirm_send` is a broken approval path: rows are not resolved/listed by the normal approval resolver.  
  Evidence: `config/tools.yaml:2775`, `config/tools.yaml:2803`, `config/tools.yaml:2831`, `tools/gatekeeper.py:105`, `tools/approvals.py:185`, `agents/telegram_bridge.py:2248`.

- Progress, voice, photo, and Notes are model-controlled outbound/write channels that bypass normal final-send filtering or external-data review.  
  Evidence: `tools/runtime/progress.py:134`, `tools/voice_outbound.py:102`, `tools/photos/_shared.py:114`, `tools/apple_notes/create.py:39`.

### Delivery And Persistence

- Telegram send success plus DB persist failure is reported as success; handoff/reflection can proceed without the final text row.  
  Evidence: `agents/messaging.py:186`, `agents/messaging.py:195`, `agents/messaging.py:201`, `agents/telegram_bridge.py:568`, `agents/telegram_bridge.py:579`.

- Photo/voice episodes are written even when reply delivery fails.  
  Evidence: `agents/telegram_bridge.py:565`, `agents/telegram_bridge.py:993`, `agents/telegram_bridge.py:996`, `agents/telegram_bridge.py:1168`, `agents/telegram_bridge.py:1171`.

### Memory And Reflection

- Reflection facts cite `source_message_id` but do not verify that the row exists or supports the claim; supersession can rewrite memory from LLM YAML.  
  Evidence: `agents/reflection.py:208`, `agents/reflection.py:269`, `agents/reflection.py:276`, `agents/reflection.py:293`, `agents/reflection.py:327`.

- `self_model` reflection output bypasses sanitizer and is injected into prompt context.  
  Evidence: `agents/reflection.py:445`, `agents/hooks.py:911`, `agents/peer_model.py:98`.

- Fact invalidation can report success for nonexistent facts or dangling replacements.  
  Evidence: `tools/memory/mark_fact_invalid.py:27`, `storage/db.py:2162`, `storage/db.py:600`.

### Persona, Companion Safety, And Evals

- Romantic exclusivity, jealousy, scarcity, and proactive absence pressure need explicit user-agency boundaries.  
  Evidence: `assets/PERSONA.md:6`, `assets/PERSONA.md:7`, `assets/PERSONA.md:81`, `assets/PERSONA.md:174`, `assets/PERSONA.md:188`.

- Deliberately fuzzy recall currently encourages wrong-but-close memory and conflicts with "do not fabricate."  
  Evidence: `assets/PERSONA.md:247`, `assets/PERSONA.md:253`, `assets/PERSONA.md:436`.

- Anti-sycophancy boundary checks are narrow or post-send. Attachment escalation appears detected but not wired into the main outbound filter.  
  Evidence: `agents/post_filter.py:259`, `agents/post_filter.py:274`, `agents/post_filter.py:843`, `agents/post_filter.py:852`.

- Layer C judges authored transcripts more than live generated behavior; multi-turn scoring drops most transcript context.  
  Evidence: `evals/conversation/runner_layer_c.py:50`, `evals/conversation/runner_layer_c.py:98`, `evals/conversation/runner_layer_c.py:107`, `evals/conversation/scorer.py:64`.

### Proactive, Research, And Telegram UX

- `sender.send` charges every engagement send to the user-anchored budget and drops candidate reason metadata, starving `/proactive why`.  
  Evidence: `agents/engagement/sender.py:151`, `agents/engagement/sender.py:162`, `agents/proactive_gate.py:207`, `agents/proactive_gate.py:212`.

- Quiet hours ignore `interruption_right: high`, so time-sensitive reminders/calendar prep cannot wake despite policy.  
  Evidence: `config/engagement.yaml:1025`, `config/engagement.yaml:1074`, `agents/engagement/guard.py:38`, `agents/proactive_gate.py:231`.

- Producer `mark_consumed` contracts are inconsistent; scheduler calls `mark_consumed(candidate)`, but several producers expect no args or raw ids.  
  Evidence: `agents/scheduler.py:562`, `agents/engagement/producers/anniversary_callback.py:121`, `agents/engagement/producers/research_callback.py:68`, `agents/engagement/producers/belief_resurface.py:59`.

- Deferred research summaries lose citation/trust boundaries and transient failures become permanent "(no useful sources)".  
  Evidence: `agents/subagents/research_worker.py:88`, `agents/subagents/research_worker.py:107`, `agents/subagents/research_worker.py:165`, `agents/subagents/research_worker.py:176`, `agents/engagement/producers/research_callback.py:57`.

- Telegram keyboards/callbacks are partially unwired or page-index inconsistent.  
  Evidence: `agents/cockpit.py:1373`, `agents/telegram_bridge.py:2861`, `agents/cockpit.py:1253`, `agents/telegram_bridge.py:2812`, `agents/telegram_bridge.py:2763`, `agents/cockpit.py:1211`, `agents/telegram_bridge.py:2663`.

## Suggested Implementation Order

1. **Stop persistence and security footguns.** Gate or remove `skill_approve`/`run_skill`, fail closed for unregistered tools, fix session-id two-phase commit, and attach safe logging filters to handlers.
2. **Make evals trustworthy.** Fix Layer C threshold scale, add full-transcript scoring, add a fast deterministic anti-sycophancy/security lane, and convert high-risk authored fixtures into generated-output trajectories.
3. **Add hard override stack.** Safety, truth, tool fidelity, user agency, and crisis mode must override character voice, romance, silence, terseness, and flirt.
4. **Normalize proactive/research contracts.** One source registry, `mark_consumed(candidate)`, candidate reason metadata, pool-aware cadence, structured cited research, and source-aware quiet hours.
5. **Harden memory.** Add evidence validators for reflection facts, sanitize self-model, validate invalidation/supersession, centralize bitemporal activity checks.
6. **Polish Telegram UX.** Fix keyboard/callback contracts, suppress playful reactions in vulnerable contexts, and route progress through filtered/templated status.

## Research Sources Used By Review Lanes

Key research references included Persona-Chat, RoleBench, InCharacter, SOTOPIA, PersonaGym, CharacterBench, SycEval, DMT-RoleBench, ELEPHANT, SYCON Bench, PersonaLens, RMTBench, PersonaEval, PICon, MREval, BeliefShift, Anthropic sycophancy/persona-selection posts, OpenAI sycophancy postmortem, companion-safety reviews, Replika update studies, FTC/EU/California companion-chatbot policy material, and Common Sense Media companion safety material. The detailed source list is in `codex/hikari-agent-review-lane-findings-2026-05-28.md`.

