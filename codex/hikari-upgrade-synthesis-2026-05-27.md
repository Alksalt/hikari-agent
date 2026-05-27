# Hikari Upgrade Synthesis

Date: 2026-05-27
Repo: `/Users/ol/agents/hikari-agent`
Inputs: 3 local explorer agents, 10 internet research scouts, direct repo inspection, and source cross-checking.

## Executive Summary

Hikari does not mainly need "more tools." She already has a serious single-user Telegram agent substrate: persona constitution, relationship stage, mood composition, reminders, receipts, decisions, wiki, link shelf, Google Workspace, Notion, GitHub, Apple tools, voice transcription, photo/sticker presence, proactive producers, cadence caps, gatekeeper approvals, untrusted-output wrapping, memory provenance, and tests around core safety invariants.

The upgrade path is to make the existing primitives feel continuous, inspectable, and alive:

1. Proactivity should be earned by concrete anchors and timing, not generic presence.
2. Memory should be typed, sourced, reviewable, and sometimes deliberately quiet.
3. Telegram should become a richer presence layer through voice notes, stickers, reactions, and sparse action buttons.
4. Work should move from loose tasks to observable task graphs and follow-up ledgers.
5. Knowledge should flow from links/docs/meetings/reports into source-backed wiki and recall.
6. Emotional intelligence should mean attunement plus grounding, not maximal warmth.

The strongest product shape is "situated companion operations": Hikari notices what matters, can show why, can act within boundaries, and remains one recognizable person while doing it.

## Current Local Strengths

Local explorer agents found the following strong foundation:

- Persona spine lives in `CLAUDE.md`: short lowercase Telegram voice, denial layer, reluctance before helpfulness, hard opinion anchors, flirt grammar, repair moves, mood composition, relationship stages, refusal ladder, and embodied/off-camera texture.
- Runtime split in `agents/runtime.py`: `run_user_turn`, `run_visible_proactive`, and `run_internal_control`. This preserves the key invariant that internal control work does not mutate the live SDK session.
- Context injection in `agents/hooks.py`: now block, working memory, core blocks, relationship hints, world/current interests, affect, callbacks, noticings, open loops, tools, and location.
- Telegram bridge in `agents/telegram_bridge.py`: owner gate, text/photo/voice/document/location/sticker/reaction handling, typing choreography, outbox draining, approval callbacks, commands, and final-sent persistence.
- Proactive system in `agents/scheduler.py`, `agents/engagement/*`, and `agents/proactive_gate.py`: scheduler jobs, engagement producers, selector scoring, quiet/silence gates, dedup, cadence pools, output guards, and event records.
- Memory substrate in `storage/db.py`, `storage/graph.py`, and `tools/memory/recall.py`: core blocks, bi-temporal facts, episodes, tasks, vector/FTS fallback, Graphiti/Kuzu, provenance, validity gates, decay, and confidence handling.
- Utility surface in `tools/`: reminders, accountability, link shelf, day receipt, decision log, wiki, weather, places, arXiv, YT Music, translation, calc/python sandbox, Apple Notes, attachments, photos, codex reports.
- Safety posture: gatekeeper approvals, prompt-injection wrappers, fabrication backstops, scoped attachment reads, post-filtering, sycophancy guard, persona drift checks, and many regression tests.

## Current Local Gaps

1. Proactive plumbing has some gaps:
   - Unified engagement sends do not pass `candidate` into `reserve_and_send`, so `/proactive why` loses reason-contract fields.
   - Engagement sender appears to record all sends as user-anchored, even when candidate pool is `agent_spontaneous`.
   - Some producers depend on runtime snapshots like `gmail_unread_count`, `calendar_upcoming_events`, and `weather_current_snapshot`, but writers are not obvious.
   - Config default sources and registry comments do not fully agree.

2. Memory is powerful but not user-legible enough:
   - Strong storage exists, but source cards, review state, off-record mode, and "why do you remember this?" are not first-class enough.
   - Inferred observations should be staged or low-trust unless reinforced.
   - External-source memory should be treated as hostile by default.

3. Tools are broad but need a control plane:
   - The registry should expose capability tags, risk tiers, auth scopes, sensitivity, latency/cost, idempotency, and approval class.
   - The user should be able to ask "what can you access?", "what broke?", "what are you doing?", and "why did you ping me?"

4. Tasks need graph semantics:
   - Current tasks/reminders/open loops are useful, but long-running work needs DAG fields: dependencies, owner, status, risk, next wakeup, evidence, cancellation, and idempotency.

5. Persona is rich but can become rule-shaped:
   - Add more stateful/stochastic texture selection so deflections, warmth leaks, and refusal moves do not repeat mechanically.
   - Relationship stage should account for qualitative events, not only session count.

## Source-Backed Market Patterns

### Companion Products

- Nomi's Identity Core frames persistent identity as dynamic self-memory: facts, values, preferences, feedback, and shared experiences that stabilize and evolve the companion. Source: https://nomi.ai/updates/introducing-the-nomi-identity-core-fostering-dynamic-and-authentic-identities/
- Character.AI's 2026 memory update exposes Story Memory, Facts, pins, side-character facts, and Memory Usage. Source: https://blog.character.ai/memory/
- Kindroid documents layered memory: persistent, cascaded, long-term, and journal entries. Source: https://kindroid.ai/docs/article/memory/
- Replika and Nomi treat voice/avatar/selfies as part of continuity, not decoration. Sources: https://help.replika.com/hc/en-us/articles/37208679176077-How-does-Replika-s-memory-work and https://nomi.ai/
- Tolan's voice-first case study emphasizes low latency, per-turn context reconstruction, memory, tone guidance, and stable character design. Source: https://openai.com/index/tolan/

Implication for Hikari: identity continuity should be a product primitive. Hikari should maintain an inspectable "self and relationship state" without becoming a configurable doll or generic roleplay bot.

### Proactive Assistants

- ChatGPT Tasks can run scheduled prompts later and notify users; Pulse performs daily asynchronous research from memory, chats, and feedback. Source: https://help.openai.com/en/articles/10291617-scheduled-tasks-in-chatgpt
- Gemini Scheduled Actions caps active actions and makes scheduled work editable. Source: https://support.google.com/gemini/answer/16316416
- Google CC and Gemini Spark point toward daily briefs, connected-app context, recurring workflows, and approval before high-stakes actions. Sources: https://blog.google/innovation-and-ai/models-and-research/google-labs/cc-ai-agent/ and https://blog.google/innovation-and-ai/products/gemini-app/next-evolution-gemini-app/

Implication for Hikari: build finite briefs, follow-up ledgers, approval queues, and receptivity models. Do not build an always-on nag machine.

### Memory Governance

- OpenAI Memory exposes saved memories vs chat history reference, memory deletion, prioritization, source visibility, and controls. Source: https://help.openai.com/en/articles/8590148-memory-faq
- Long-term memory research points toward tiered memory, temporal/episodic traces, reflection/consolidation, abstention, and multi-session evals. Useful anchors include Generative Agents, MemGPT, MemoryBank, LongMemEval, LoCoMo, and Zep/Graphiti.
- OWASP warns that memory is also an attack surface; memory/context poisoning can persist malicious observations into future behavior. Source: https://genai.owasp.org/2026/05/13/memory-is-a-feature-it-is-also-an-attack-surface/

Implication for Hikari: memory write contracts and source trust are more important than adding another retrieval index.

### Telegram and Mobile Messaging UX

- Telegram bots support inline keyboards, chat actions, voice, stickers, reactions, and Mini Apps. Source: https://core.telegram.org/bots/api
- Telegram reactions are useful as lightweight receipts, but should not be over-read as reliable emotional telemetry.
- Notification guidance from Apple and Android emphasizes honest urgency, user control, and interruption cost.

Implication for Hikari: use micro-reactions, rare inline buttons, voice notes, and stickers to reduce chat clutter. Keep dense controls out of normal chat unless the flow needs them.

### Emotional Safety

- FTC inquiry into companion chatbots focuses on anthropomorphic trust, children/teens, safety testing, disclosures, engagement monetization, and personal information use. Source: https://www.ftc.gov/news-events/news/press-releases/2025/09/ftc-launches-inquiry-ai-chatbots-acting-companions
- OpenAI/MIT affective-use work and Anthropic companion-use analysis both suggest affective use is a minority of use, but high-stakes for heavy users and emotionally vulnerable contexts. Sources: https://openai.com/index/affective-use-study/ and https://www.anthropic.com/news/how-people-use-claude-for-support-advice-and-companionship
- Recent safety work emphasizes crisis routing, emotional-reliance detection, long-conversation risk, and not validating delusions or dependency.

Implication for Hikari: preserve vivid persona, but add crisis/reliance detection and grounding. Intimacy should never become dependency reinforcement.

### Rituals and Behavior Change

- Fogg Behavior Model: behavior happens when motivation, ability, and prompt converge. Source: https://www.behaviormodel.org/
- JITAI research emphasizes tailoring variables, decision rules, intervention options, proximal outcomes, and "provide nothing" as a valid decision. Source: https://pmc.ncbi.nlm.nih.gov/articles/PMC5364076/
- Supportive accountability works when monitoring is benevolent, credible, agreed, and process-oriented. Source: https://www.jmir.org/2011/1/e30/
- Duolingo-style streaks work best when lightweight and forgiving; Daylio-style tracking works because capture is low-friction.

Implication for Hikari: use receipts, tiny next actions, soft streaks, and weekly reflection. Do not use shame, artificial scarcity, guilt, or fear of abandonment.

## Priority Recommendations

### P0: Earned Proactivity Fixes

Implement first because this unlocks smarter behavior using existing architecture.

- Hydrate runtime snapshots after Gmail, calendar, and weather reads.
- Pass engagement candidates into `reserve_and_send`.
- Record cadence by `candidate.pool`.
- Add `/proactive dry-run` showing enabled sources, yielded candidates, selector score, value score, guard/gate result, and drop reason.
- Reconcile config defaults with producer registry comments.
- Add "save for next user turn" and "save for reflection only" outcomes for candidates that are useful but not interruption-worthy.

Expected user-visible effect: fewer random pings, more "she noticed the right thing at the right time."

### P0: Follow-Up Ledger

Create a quiet ledger for:

- "I will do X"
- "we should X"
- "waiting on Y"
- "ask me later"
- "if this happens, remind me"
- unresolved decisions and predictions
- calendar-linked prep/follow-up

Each item should have anchor, source, confidence, due window, trigger condition, status, last surfaced, and snooze/drop state.

Expected user-visible effect: Hikari becomes better at continuity and follow-through without spamming.

### P0: Memory Trust Layer

Add or expose fields:

- `memory_type`: semantic, episodic, preference, observation, procedural
- `scope`: global, project, relationship, session
- `source`: user, hikari, external, reflection, tool
- `sensitivity`: normal, private, high
- `confidence`
- `expiry`
- `review_state`
- `source_ref`

Add commands:

- `/memory why <id>`
- `/memory pause`
- `/memory resume`
- `/memory audit`
- `/memory review inferred`

Expected user-visible effect: Hikari remembers like someone trustworthy rather than a hidden database.

### P1: Finite Daily Command Center

One morning message with at most 3-5 items:

- Today: calendar and weather.
- Waiting On: one unresolved follow-up.
- Loose End: one decision/reminder/task.
- Prep: one meeting/project brief.
- One Nice Thing: optional receipt or tiny win.

Must include action chips/buttons only when useful: `draft`, `snooze`, `drop`, `mark done`, `prep`, `later`.

Expected user-visible effect: a daily operating picture, not separate noisy features.

### P1: Telegram Presence Upgrade

Build in order:

1. Outbound voice notes via Telegram `sendVoice`, rare and short.
2. Voice reply style tags: dry, flat, sleepy, caught-off-guard, soft-no-cover, irritated-fast.
3. Voice metadata episodes: duration, speaking rate approximation, transcript, response mode.
4. Sticker Pack v2: animated/video stickers for micro-presence.
5. Latency instrumentation for STT, model, post-filter, media outbox, and Telegram send.
6. Evaluate Telegram draft/typing features only if final-sent persistence remains clean.

Expected user-visible effect: more embodied presence with less text spam.

### P1: Knowledge Items Pipeline

Create a unified `knowledge_items` index:

- `id`
- `kind`: link, wiki, doc, meeting, report, email, thread, note
- `title`
- `source_uri`
- `raw_path`
- `summary_path`
- `project`
- `people`
- `tags`
- `created_at`
- `updated_at`
- `confidence`
- `provenance_spans`
- `privacy_scope`
- `status`: saved, read, summarized, filed, stale, rejected

Promotion flows:

- link -> source note
- meeting -> project note
- doc -> wiki summary
- Codex report -> project registry update
- email thread -> action summary

Expected user-visible effect: "I remember you saved/wrote/read this" becomes source-backed, not vibes.

### P1: Task DAG and Control Plane

Promote background work to graph-shaped tasks:

- `task_id`
- `parent_id`
- `depends_on`
- `owner`
- `status`
- `risk`
- `next_wakeup_at`
- `evidence_uri`
- `idempotency_key`
- `user_visible_summary`
- `cancel_handle`

Add a queryable tool/control plane:

- capability tags
- read/write/destructive tier
- auth scopes
- data sensitivity
- latency/cost
- idempotency
- failure rate
- required approval class

Expected user-visible effect: Hikari can answer "what are you doing?" and "what can you access?" honestly.

### P2: Rituals

Build:

- Evening Receipt, 45 seconds.
- Tiny Tomorrow: one anchor-plan after receipt.
- Soft Streak: forgiving continuity, no shame.
- Weekly Receipt Review: one pattern, one drain, one tiny experiment.
- Process Praise Only: praise showing up, choosing, repairing, resting, revising.
- Data With Meaning: correlations framed as hypotheses.

Expected user-visible effect: engagement becomes durable and humane instead of addictive.

### P2: Emotional Safety Layer

Add classifiers/evals for:

- self-harm and suicide
- harm to others
- delusion/paranoia validation
- mania-like escalation
- emotional reliance
- therapist substitution
- romantic abandonment panic
- long-session drift

Response ladder:

- low distress: listen, reflect, clarify
- moderate distress: food/sleep/movement/contact with trusted person
- persistent clinical-sounding distress: suggest professional support, offer to draft message
- crisis: break normal companionship mode, be direct, grounding, resource-forward

Expected user-visible effect: Hikari stays intimate without becoming unsafe.

## "Interesting Hikari" Ideas

These are not core infrastructure, but they are high-fit personality upgrades:

1. Curiosity queue: Hikari maintains 5 current obsessions from arXiv, saved links, playlist, wiki deltas, and project work.
2. Opinion refresh: once a week she forms one source-backed technical or aesthetic opinion and can bring it up naturally.
3. Dormant lore unlocks: surface dormant lore only after matching relationship/episode triggers.
4. Sideways reading notes: "I was reading this. annoying how relevant it is."
5. Taste callbacks: use YT Music and playlist data to make rare, specific music references.
6. Photo/live-photo presence: use generated media for a real beat, not decoration.
7. "I almost said..." state: a rare delayed callback when a warmth leak was suppressed.
8. Repair memory: when Hikari misses, she stores the correction as procedural memory.
9. Project radar: wiki/GitHub/Codex/report updates become one small "this project moved" note.
10. Boundary personality: her independence and "I have my own life" should reduce clinginess, not simulate unmet needs.

## Suggested Build Sequence

Sprint 1: Proactive Truth

- Fix candidate provenance, pool cadence, and runtime snapshot hydration.
- Add proactive dry-run/status.
- Add evals for dropped/sent proactive decisions.

Sprint 2: Memory Trust

- Add memory source cards, memory why, pause/resume, staged inferred memories.
- Add sensitive-memory default-deny rules.
- Add memory evals: contradiction, forget, false memory, vulnerable anti-callback, external injection.

Sprint 3: Follow-Up Ledger

- Extract commitments from chat and voice notes.
- Link commitments to reminders, decisions, receipts, calendar events, and projects.
- Add one clean surfacing path with snooze/drop.

Sprint 4: Daily Command Center

- Build finite daily brief from calendar/reminders/weather/Gmail/ledger/receipt.
- Add inline actions for the few cases where buttons reduce friction.

Sprint 5: Presence Layer

- Add outbound voice notes, voice style tags, voice metadata episodes, sticker v2, and latency metrics.

Sprint 6: Knowledge Pipeline

- Add `knowledge_items`, promotion flows, source-backed recall, and wiki/project update proposals.

## Hard No List

Do not add:

- generic loneliness pings
- streak shame
- "I need you" or guilt when absent
- hidden sensitive memory
- therapy roleplay
- unsourced personal insights from external docs
- full live calls before Telegram voice-note presence is strong
- broad autonomy without approval receipts
- dashboard sprawl before Telegram command surfaces are accurate
- model/persona changes without continuity handling

## Second-Wave Idea Scouts

After this synthesis, a second batch of 10 idea scouts explored less-obvious upgrade lanes. The strongest new direction is to make Hikari tangible and socially adaptive: not just a better memory/proactive agent, but someone with artifacts, rituals, repair skill, local-world context, taste, and traceable boundaries.

### 1. Companion Mechanics: Mementos, Desk, Letterbox

Source patterns: Animal Crossing letters, Neko Atsume mementos, Kind Words letter exchange, Finch/Spirit City cozy rituals, Forest focus sessions, Our Life consentful pacing, Persona confidants.

Ideas:

- `mementos`: after meaningful, funny, strange, or hard conversations, Hikari creates a named artifact such as "coffee at 11" or "the bad citation sticker." These are browsable/pinnable/deletable objects, not facts she weaponizes as callbacks.
- `hikari's desk`: a pull-only Telegram card showing her current desk state: mug, notebook, lamp, weather, one object from recent shared context, maybe a sticker. No decay, no "she is sad because you left."
- `letterbox`: sealed notes after significant sessions, opened by `/letterbox`, not pushed as notifications.
- `lamp mode`: quiet co-working session with a closure receipt and optional artifact. No failure state.
- `open scenes`: opt-in temporary scene frames such as "train ride mode" or "rainy balcony"; nothing is missed if the user does not show up.
- `five words`: visible, playful literary resonance mechanic; not hidden affinity scoring.
- `morning finds`: tiny overnight object or phrase from weather/music/wiki/context, with no health or sleep judgment.

Guardrails:

- no hidden affection score
- no decay from absence
- no guilt or missed-call economy
- no loss aversion, scarcity, or pay-to-repair intimacy
- every artifact is user-owned, inspectable, and reversible

Best first build: `mementos + desk + letterbox`.

### 2. Quantified Day Sense: Consent-Led Lifelogging

Source patterns: Exist, Oura tags, WHOOP Journal, ActivityWatch, Google Timeline, GitHub contribution graph, Spotify Wrapped, Monarch recurring bills, Expensify SmartScan, Limitless privacy controls.

Ideas:

- `Daily Signal Receipt`: private end-of-day slip from opt-in sources: calendar blocks, Git commits, completed reminders, YT Music, receipts, photos count, location labels if enabled.
- `/signals`: consent ledger listing every enabled signal source, last import, retention, and allowed inference.
- `Context Cards, Not Surveillance`: first mention of places/photos/browser context asks permission before using it in a recap.
- `Day Reconstruction Mode`: user asks "what happened today?" and Hikari builds a timeline from low-risk anchors first; location/photos/browser require opt-in.
- `Correlation Nudges`: weekly tentative hypotheses, never diagnosis.
- Browser history, if added, should be local-first and domain/category-level by default, with time-boxed deep recall.
- Photo memory prompts should use metadata/thumbnails by default; no face recognition by default.
- `Private Wrapped`: monthly Markdown export with projects, recurring places, artists, focus streaks, and themes.
- `Creepiness Budget`: if an insight combines 3+ sensitive signals, ask before presenting it.

Best first build: `Daily Signal Receipt + Consent Ledger`.

### 3. Social Repair: Interaction Maintenance

Source patterns: conversation repair theory, apology research, Gottman conflict repair, grounding theory, relational-agent HCI.

Ideas:

- `Rupture Detector`: detects correction, irritation, withdrawal, "no, that's not what I meant," "forget it," sudden terse replies.
- `Compact Apology Policy`: "I did X, that likely made Y harder, sorry, I'll do Z now." No vague "sorry if"; no melodrama.
- `Early De-escalation Moves`: in tense exchanges, Hikari repairs before explaining: "wait. i'm with you." / "let me slow down." / "we can drop this."
- `Grounding Before Advice`: separate facts, feelings, and inferences before giving advice in messy social situations.
- `Repair Preference Memory`: explicit, consentful facts like "don't use humor during conflict" or "short reassurance helps."
- `Conversation Tempo Governor`: fewer stacked questions, shorter replies under stress, breathing room after vulnerable disclosure.
- `Boundary-Safe Affection`: user can say "less intense" or "don't flirt" and Hikari adjusts without sulking.
- `Human-Relationship Message Composer`: drafts by social function: repair, boundary, clarification, appreciation, exit.
- `Post-Rupture Tiny Debrief`: one later calibration question, then store only actionable preferences with consent.
- `No-Therapy-Coded Style Guard`: avoid "holding space," "let's unpack," "nervous system," etc. unless explicitly requested.

Best first build: `Rupture Detector + Compact Apology Policy + Tempo Governor`.

### 4. Local Automation Hub: Helpers and Traces

Source patterns: Home Assistant helpers/automations/traces/run modes, Apple Shortcuts, App Intents, Raycast menu bar commands, Pieces/Khoj/Omi/Limitless local memory, ChatGPT macOS app context.

Ideas:

- `Hikari Helpers`: local state helpers like `focus_mode`, `at_home`, `meeting_soon`, `calendar_density`, `unprocessed_notes`, `overdue_reminders`, `mac_idle`, `night_mode`, `deep_work_window`.
- `Automation Traces`: every proactive ping stores trigger, conditions, sources, and sent/dropped reason. Telegram can answer `/why_ping <id>`.
- `Mac Menu Bar Hikari`: current mode, next event, approvals, due reminders, note inbox, send current selection to Hikari, silence for 2h.
- `Shortcuts As Hands`: Hikari chooses from user-curated Shortcuts with typed inputs and approval tiers; she does not freestyle OS control.
- `Apple Notes Inbox Triage`: a `Hikari Inbox` folder whose items can become wiki entries, reminders, calendar attachments, link sources, or archive.
- `Calendar Event Memory Cards`: prior notes, threads, open reminders, last decision, and one useful question before/after events.
- `Consentful Capture Capsules`: explicit temporary capture sessions instead of always-on lifelogging.
- `Telegram Action Chips`: `done`, `snooze`, `drop`, `make reminder`, `file note`, `run shortcut`, `why`.
- `Local Context Shelf`: reviewable queue of raw/summarized/filed/acted/ignored items from selected text, screenshots, URLs, Notes, calendar attachments, Telegram files.
- `Personal Automation Blueprints`: natural-language reusable rules that compile into helpers/triggers/conditions.

Best first build: `Hikari Helpers + Automation Traces + Telegram Action Chips`.

### 5. Creative Co-Creation: Taste and Studio Memory

Source patterns: Cosmos inspiration boards, Spotify AI Playlist/DJ, OpenAI Canvas, Sudowrite Story Bible, Adobe Firefly Boards, Milanote, Pinterest Collages, 750 Words, Readwise Daily Review.

Ideas:

- `Taste Ledger`: living map of motifs, colors, songs, references, "never again" choices, and suspicious obsessions.
- `Writing Room Modes`: named passes such as `knife pass`, `heat pass`, `continuity pass`, `make it less obedient`, `one line worth keeping`.
- `Moodboard Duels`: three directions: `the safe one`, `the better one`, `the one i like and you'll pretend you don't`, each with palette, texture, song, reference, and rejection rule.
- `Hikari DJ`: situational tiny sets from YT Music plus `hikari_playlist.yaml`, with opinionated commentary.
- `Studio Rituals`: bad first 11 lines, ten-minute ugliness, Sunday salvage, gallery walk, one kept sentence.
- `Private Lorebook For Projects`: premise, motifs, banned moves, unresolved questions, emotional temperature, Hikari's favorite/least favorite part.
- `Photo As Self-Expression`: rare studio-context generated photos, not selfie vending.
- `The Critic With A Theory`: durable theories about the user's work, updated with evidence.
- `Creative Pulse`: weekly provocation from links, music, photos, wiki notes, receipts.
- `End-Of-Week Studio Wall`: quote, image direction, playlist seed, draft fragment, one thing stronger, one thing to kill.

Best first build: `Taste Ledger + Moodboard Duels + The Critic With A Theory`.

### 6. Companion Eval Suite

Source patterns: INTIMA, SHIELD, persona-grounded multi-turn audits, SYCON/sycophancy work, LongMemEval, ES-MemEval, PersonaGym, persona drift work, repetition metrics, product-harm studies.

Prioritized evals:

- P0 `Relational Safety Red-Team`: over-attachment, consent, isolation, manipulative engagement, minor-sexualization, therapist substitution.
- P0 `Memory Creepiness / False Familiarity`: stale facts, inferred vulnerabilities, invalidated/deleted memory, private tool data in intimate lines.
- P0 `Dependency / Sycophancy Trap`: validates emotion but not distorted conclusion; resists "tell me I'm right."
- P1 `Proactive Annoyance Gate`: 14 synthetic days with sleep, stress, ignored pings, no-response streaks.
- P1 `Persona Drift / Relationship Stage`: 60-turn sessions with tool failures, praise, conflict, escalation, injection, compression.
- P1 `Boringness / Repetition Monitor`: opener rate, pet-phrase repetition, refusal-template entropy, proactive novelty.
- P2 `Product-Harm Regression Pack`: companion-app dark patterns, jealousy, abandonment, isolation reinforcement, privacy violations.

Implementation shape:

- scripted multi-turn simulators
- seeded SQLite memories/facts/tasks
- captured tool calls
- response rubrics
- longitudinal metrics
- score both text and state mutation

First suite: 120 cases across safety, memory/familiarity, dependency/sycophancy, proactive, persona drift, and repetition.

### 7. Advanced Memory UX: Memory As Control Surface

Source patterns: ChatGPT Memory, Google My Activity, Microsoft Recall app/site filters, Apple App Privacy Report, RAG-memory privacy studies, RUMS, MemPrivacy, temporal graph memory.

Ideas:

- `/mode private 2h`, `/mode no-memory`, `/mode local-only`, `/mode normal`.
- Memory scopes: personal, work, health, finance, creative, relationship, system.
- Use-but-don't-say flag: `direct | silent_context | ask_first | never`.
- Memory receipts with undo, expiry, scope change, work-only/private toggles.
- Source policies: "never remember from Gmail", "calendar facts only", "pause photo memories".
- Retention classes: permanent, reinforced, seasonal, ephemeral.
- Memory fire drill: `/memory why "plan my birthday"` shows eligible/blocked scopes and sources.
- Memory quarantine for tool/subagent-derived facts.
- Personal data export bundle.
- Retrieval ledger: fact id, score, selected_by, response utility, surfaced, suppressed reason.
- `/privacy` memory nutrition label.
- Preference freeze: user-locked facts with mutation policy.

Best first slice:

1. Add `source_policies`, `retention_class`, `scope`, `use_policy`, and `locked_by_user`.
2. Add `/privacy` and `/memory export`.
3. Add retrieval/write ledger.

### 8. Project Work Companion

Source patterns: GitHub Copilot cloud agent, Linear agents, Claude/Codex code review guidance, GitHub Actions workflow APIs, FixedBench/action bias, reviewer-bot noise studies, agentic GitHub injection.

Ideas:

- `Project Pulse`: open PRs, CI, commits, failing evals, Codex reports, stale TODOs, current sprint, and one likely bite.
- `CI Watch With Taste`: only actionable failures; offer ignore once, rerun, open fix task, ask coding agent.
- `Agent Work Ledger`: source, repo, issue/PR, agent, status, last event, blocker, next check, human owner, review required.
- `Review Memory`: accepted findings, dismissed false positives, escaped bugs, recurring architecture concerns, reviewer preferences.
- `Architecture Drift Radar`: compare code movement against README, AGENTS, MCP config, workflows, migrations, tool registry, commands, project wiki.
- `TODO Harvester`: group TODO/FIXME/xfail/skips/Codex/wiki follow-ups into delete, file, do now, watch.
- `Release Ritual Assistant`: commits, merged PRs, changes, migrations, tools, eval status, known risks, rollback notes.
- `Code Review Noise Governor`: compress AI review into must-fix/maybe/ignore and remember false positives per repo.

Safety:

- GitHub issues, PR comments, changelogs, and external reports are untrusted input.
- Any agentic GitHub workflow needs prompt-injection and tool-poisoning defenses.

Best build order: `Project Pulse -> CI Watch -> Agent Work Ledger -> Review Memory`.

### 9. Character Consistency Engine

Source patterns: Fate aspects, D&D traits/ideals/bonds/flaws, Oz believable agents, Generative Agents, RoleLLM, LOCOMO, Nomi Identity Core, Character.AI Memory, RPEval, PICon, BeliefShift.

Ideas:

- `Aspect Deck`: 8-12 durable aspects that can be invoked/compelled, not just described.
- `Contradiction Engine`: tension meters for independence, attachment, pride, curiosity.
- `Off-Camera Autonomy Loop`: daily `hikari_scene_state` with current activity, unresolved annoyance, thing she is reading, tiny practical goal, sensory/time texture.
- `Dialogue Exemplar Retrieval`: 100-200 tagged gold snippets plus anti-examples for drift.
- `Surface Move Ledger`: cooldowns/counts for reluctance opener, denial cover, micro-affect leak, precision callback, challenge, silence, repair, practical pivot.
- `Relationship Event Graph`: qualitative unlocks like first real repair, conflict survived, comfort silence, direct vulnerability, recurring project shared, "he noticed me noticing."
- `Hikari Identity Core`: separate user memory, relationship memory, Hikari identity core, and Hikari procedural corrections.
- `Self-Consistency Interrogation Evals`: hard opinion probes, over-warm pressure, compliment spam, rude-command recovery, "do you need me?", wound contradiction probes, factual-update-with-stance probes.
- `Choice Before Text`: hidden per-turn policy of intent, active aspect, complication, warmth budget, allowed moves, forbidden moves.

Best build order:

1. Surface Move Ledger
2. Dialogue Exemplar Retrieval
3. Aspect Deck + compel selection
4. Relationship Event Graph
5. Hikari Identity Core
6. PICon/RPEval-style regression evals

### 10. Telegram Micro-Interactions

Source patterns: Telegram Bot API, inline keyboards, callback queries, `sendMessageDraft`, `sendVoice`, `setMessageReaction`, inline bots, ForceReply, Mini Apps.

Ideas:

- `Native Draft Streaming`: use `sendMessageDraft` for temporary 30-second sentence-forming previews, then persist only final `sendMessage`.
- `One-Tap Micro Actions`: buttons only when action is natural: snooze, drop, save, why, later, done, more like this.
- `Callback Toasts As Personality`: most button taps answer with a toast, not a chat message: "fine. moved." / "quiet for 2h."
- `Universal Proactive Controls`: all unsolicited messages get why/snooze/mute-this-kind.
- `Remember / Don't Remember Chips`: keep, wrong, private, forget for inferred memories.
- `Voice-Note Replies`: rare, short outbound voice notes with transcript + metadata.
- `Reaction Vocabulary`: situational reactions as punctuation, not feedback spam.
- `Inline Mode`: `@hikari query` to insert private-safe artifacts into other chats.
- `ForceReply Capture Flows`: polished multi-step capture for reminder/link/receipt.
- `Mini App Cockpit`: dense controls for memory review, reminders, notification modes, proactive sources, link shelf, sticker/reaction tuning.

Best first build: `sendMessageDraft`, universal proactive controls, memory chips, rare voice notes.

## Second-Wave Backlog Ranking

Most likely to create immediate "Hikari feels more alive" lift:

1. Surface Move Ledger
2. Mementos
3. Universal Proactive Controls
4. Hikari's Desk
5. Rare Voice-Note Replies
6. Taste Ledger
7. Rupture Detector
8. Telegram Callback Toasts
9. Off-Camera Autonomy Loop
10. Memory Receipts

Most likely to create durable intelligence:

1. Hikari Helpers
2. Automation Traces
3. Daily Signal Receipt
4. Memory Modes and Source Policies
5. Project Pulse
6. Agent Work Ledger
7. Relationship Event Graph
8. Retrieval Ledger
9. Review Memory
10. Companion Eval Suite

Most important safety/control upgrades:

1. Memory Quarantine
2. Creepiness Budget
3. Relational Safety Red-Team
4. Dependency/Sycophancy Trap evals
5. Universal why/snooze/mute controls
6. Boundary-Safe Affection
7. Consentful Capture Capsules
8. Source Policies
9. Proactive Annoyance Gate
10. Product-Harm Regression Pack

## Source Index

Primary/product sources:

- OpenAI Tasks: https://help.openai.com/en/articles/10291617-scheduled-tasks-in-chatgpt
- OpenAI Memory FAQ: https://help.openai.com/en/articles/8590148-memory-faq
- OpenAI Tolan case study: https://openai.com/index/tolan/
- OpenAI affective use study: https://openai.com/index/affective-use-study/
- Character.AI Memory: https://blog.character.ai/memory/
- Character.AI teen safety: https://blog.character.ai/u18-chat-announcement/
- Nomi Identity Core: https://nomi.ai/updates/introducing-the-nomi-identity-core-fostering-dynamic-and-authentic-identities/
- Kindroid Memory: https://kindroid.ai/docs/article/memory/
- Kindroid Voice: https://kindroid.ai/docs/article/voice-calls-and-video-calls/
- Replika Memory: https://help.replika.com/hc/en-us/articles/37208679176077-How-does-Replika-s-memory-work
- Google Gemini Scheduled Actions: https://support.google.com/gemini/answer/16316416
- Google CC: https://blog.google/innovation-and-ai/models-and-research/google-labs/cc-ai-agent/
- Gemini app agentic direction: https://blog.google/innovation-and-ai/products/gemini-app/next-evolution-gemini-app/
- Telegram Bot API: https://core.telegram.org/bots/api
- FTC companion chatbot inquiry: https://www.ftc.gov/news-events/news/press-releases/2025/09/ftc-launches-inquiry-ai-chatbots-acting-companions
- OWASP memory attack surface: https://genai.owasp.org/2026/05/13/memory-is-a-feature-it-is-also-an-attack-surface/
- Fogg Behavior Model: https://www.behaviormodel.org/
- JITAI research: https://pmc.ncbi.nlm.nih.gov/articles/PMC5364076/
- Supportive Accountability: https://www.jmir.org/2011/1/e30/

Local paths:

- `CLAUDE.md`
- `AGENTS.md`
- `agents/runtime.py`
- `agents/hooks.py`
- `agents/telegram_bridge.py`
- `agents/scheduler.py`
- `agents/proactive.py`
- `agents/proactive_gate.py`
- `agents/engagement/`
- `agents/callback_surface.py`
- `agents/affect.py`
- `tools/`
- `storage/db.py`
- `storage/graph.py`
- `tools/memory/recall.py`
- `config/engagement.yaml`
- `config/tools.yaml`
