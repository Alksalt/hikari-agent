# Prompt 7 - Product Roadmap Synthesis

Date: 2026-05-25
Repo: `/Users/ol/agents/hikari-agent`
Scope: product roadmap synthesis only. No source-code changes.

## 1. Executive summary

Hikari's product direction is a one-owner, Telegram-native personal operating layer: memory, tools, timing, voice, approvals, and proactive judgment wrapped in one consistent relationship. The product is not "ChatGPT with a character prompt" and not a general multi-user agent OS. It should win because it can check the user's actual context, do the boring coordination work, and speak with continuity.

The local repo is already past the toy stage. It has a three-way runtime split (`run_user_turn`, `run_visible_proactive`, `run_internal_control`), final-sent persistence in the Telegram bridge, owner-gated Telegram UX, 169 configured tool ids, 59 gatekeeper-gated tools, 87 untrusted-output tools, 31 write/destructive tools, reminders, receipts, decisions, link shelf, Google Workspace, wiki tools, memory validity gates, proactive engagement scoring, `/capabilities`, `/status`, `/proactive`, `/approvals`, and an initial compound-turn path.

The gap is product execution. Hikari can do many useful things, but realistic user asks arrive as messy bundles: "check tomorrow, draft the reply, remind me later, save the link, and log that I avoided the migration." That should become one work packet with parallel reads, gated writes, partial-failure receipts, and one compact final answer. Today `agents/compound_turn.py` runs extracted task strings through stateless internal-control waves, which is a useful prototype, but it lacks typed task metadata, durable work status, approval conversion, tool-result normalization, and Telegram receipt UX.

The next roadmap should therefore prioritize five moves:

1. Make existing capability legible through semantic tool/workflow surfaces, not raw tool count.
2. Promote the compound-turn prototype into a typed WorkPacket executor.
3. Make Telegram and voice render work packets cleanly: acknowledgement, progress, approval, receipt.
4. Tighten proactivity around priority, value, and "stay silent" outcomes.
5. Add evals that prove Hikari used the right tools, asked before risky writes, stayed specific, and did not spam.

External verification supports this direction. Anthropic documents that same-turn tool calls are unordered and should be treated as independent, with dependent work happening across turns or after errors are returned ([Anthropic parallel tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use)). MCP tool definitions include name, human title, description, input/output schema, and annotations, which is exactly the metadata Hikari's tool catalog still needs ([MCP tools spec](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)). Telegram provides commands, inline keyboards, callback queries, files/voice objects, and chat actions for compact product UX ([Telegram Bot API](https://core.telegram.org/bots/api)). Hermes and OpenClaw both show that capable agents need visible toolsets, background work, approvals, skills, and policy surfaces, not just more tools ([Hermes tools](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools), [OpenClaw automation](https://docs.openclaw.ai/automation), [OpenClaw tool search](https://docs.openclaw.ai/tools/tool-search), [OpenClaw exec approvals](https://docs.openclaw.ai/tools/exec-approvals)).

## 2. Research inputs reviewed

Filename note: no files matching `codex/prompt-*-2026-05-25.md` existed in the repo at review time. The `codex/` directory contained six current same-date `research-*` reports, and all six were read in full. The requested prompt filenames appear to be the intended logical names for these reports.

Reports reviewed:

- `codex/research-usefulness-non-generic-bot-2026-05-25.md` - useful/non-generic bot direction.
- `codex/research-synthesis-useful-active-bot-roadmap-2026-05-25.md` - prior roadmap synthesis, including compound-turn recommendations.
- `codex/research-active-encouragement-ux-2026-05-25.md` - proactivity and encouragement quality.
- `codex/research-tool-usefulness-audit-2026-05-25.md` - tool surface audit and workflow ranking.
- `codex/research-hermes-openclaw-competitive-2026-05-25.md` - Hermes/OpenClaw competitive teardown.
- `codex/research-telegram-voice-ux-2026-05-25.md` - Telegram and voice UX.

Local repo inspected:

- `README.md`, `CLAUDE.md`, `AGENTS.md`.
- `agents/runtime.py`, `agents/telegram_bridge.py`, `agents/compound_turn.py`, `agents/cockpit.py`, `agents/tool_inventory.py`.
- `agents/engagement/selector.py`, `agents/engagement/composer.py`, `agents/engagement/guard.py`, `agents/engagement/sender.py`, `agents/proactive_gate.py`, `agents/scheduler.py`.
- `tools/README.md`, `tools/router/tool_search.py`, `tools/dispatch/task_extractor.py`, `tools/gatekeeper.py`, `tools/approvals.py`, `tools/_response.py`, `tools/_registry.py`.
- `config/tools.yaml`, `config/engagement.yaml`.
- `.agents/skills/character-voice/`, `.agents/skills/drive-search/`, `.agents/skills/recall-memory/`, `.agents/skills/generate-photo/`, `.agents/skills/schedule-heartbeat/`.
- Relevant tests: compound turn, Telegram cockpit, callbacks, voice, reminders, receipts, gatekeeper, approval previews, post-filter fabrication, engagement/proactive, eval harness, trajectory runner, tool registry, tool inventory, and untrusted wrapping.

External sources verified:

- [Anthropic parallel tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use)
- [Claude Code subagents](https://code.claude.com/docs/en/sub-agents)
- [MCP server tools specification](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)
- [Telegram Bot API](https://core.telegram.org/bots/api)
- [Hermes Agent GitHub](https://github.com/nousresearch/hermes-agent)
- [Hermes tools and toolsets](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools)
- [OpenClaw automation](https://docs.openclaw.ai/automation)
- [OpenClaw tool search](https://docs.openclaw.ai/tools/tool-search)
- [OpenClaw exec approvals](https://docs.openclaw.ai/tools/exec-approvals)
- [Microsoft Guidelines for Human-AI Interaction](https://www.microsoft.com/en-us/research/articles/guidelines-for-human-ai-interaction-eighteen-best-practices-for-human-centered-ai-design/)
- [Fogg Behavior Model](https://www.behaviormodel.org/)
- [Supportive Accountability](https://www.jmir.org/2011/1/e30/)
- [NN/g confirmation dialogs](https://www.nngroup.com/articles/confirmation-dialog/)
- [OWASP LLM01 Prompt Injection](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)
- [OWASP LLM06 Excessive Agency](https://genai.owasp.org/llmrisk/llm062025-excessive-agency/)

## 3. Five product principles

1. Specific beats supportive.
   Every useful reply should name an anchor: event, email, file, reminder, receipt, decision, saved link, wiki page, prior phrase, or tool result. If she cannot name the object, she should usually either ask a focused question or stay quiet.

2. Tools are part of the personality.
   Hikari becomes non-generic when she checks Gmail before discussing Gmail, checks calendar before claiming a schedule, recalls memory before invoking personal history, and says when she has not checked. Tool truth is not a separate "technical" layer. It is what makes the voice feel real.

3. One user message can be a work packet.
   A Telegram text or voice note may contain several tasks. Hikari should parse it as one packet, execute independent reads in parallel, serialize dependent writes, ask for approval on risky side effects, and return one receipt.

4. Proactivity must earn the interruption.
   A proactive message needs anchor, value, timing fit, actionability, and a control path. The Fogg model's motivation/ability/prompt framing is useful here: a prompt without ability becomes friction, and a prompt without motivation becomes noise ([Fogg Behavior Model](https://www.behaviormodel.org/)).

5. Trust requires inspectability without trace leakage.
   The user should be able to ask what Hikari can access, what she is doing, what needs approval, what failed, what she remembered, and why she pinged. The answer should show actions and evidence, not hidden chain-of-thought.

## 4. Desired Hikari behavior statement

When Hikari works, she reads the whole user message, identifies the real work, checks current or personal data when needed, runs independent reads together, pauses before dangerous writes, and answers with a compact result that names what is done, what is waiting, what failed, and what happens next.

She should feel like one person, not a dashboard. The user should not have to know which tool to call. The user should feel that Hikari held the thread, reduced the next action, remembered the relevant context, and did not overstep.

Desired turn shape:

1. Detect whether the turn is single-intent or compound.
2. Use memory, wiki, tools, or web when the answer depends on personal, current, or external facts.
3. Execute independent read tasks in parallel.
4. Convert writes/sends/deletes/public actions into exact approval previews.
5. Produce one Telegram-native receipt.
6. Persist only final text that was actually delivered.

Desired proactive shape:

1. Send exact due reminders literally.
2. Prefer calendar prep, important mail, decisions, explicit follow-ups, and receipt ceremonies over mood leaks.
3. Make the next action smaller.
4. Include a "why/snooze/mute" path in the product surface.
5. Record enough metadata to explain why the message fired or why it stayed silent.

## 5. Highest-leverage user workflows

1. Morning command center.
   Calendar, reminders, weather, important Gmail, due decisions, open loops, and yesterday/today receipt context. Output: one ranked brief with buttons for "summarize thread", "prep event", "snooze", "mark done", and "log receipt".

2. Voice note to actions.
   Transcribe, segment into steps, classify risk, execute safe local/read steps, draft risky outbound steps, and send one final receipt. This is the most Telegram-native compound workflow.

3. Inbox triage and safe reply drafting.
   Find only important/actionable threads, summarize the thread that matters, draft replies, and require typed confirmation before sending. Never become an unread-count nag.

4. Calendar/event prep.
   Before a real event, fetch event details, relevant emails/docs/wiki notes, previous decisions, travel/weather only if relevant, then produce a two-minute prep card.

5. Capture from Telegram.
   URL, file, photo, voice note, location, or pasted thought becomes the right artifact: link shelf, wiki append, memory, receipt, task, reminder, or decision.

6. "Find what I know."
   Search memory, session history, wiki, links, Codex reports, and Drive when relevant. Cite where claims came from, and hedge stale or fuzzy memory.

7. Day receipt and recovery loop.
   `made`, `moved`, `learned`, and `avoided` become Hikari's main encouragement substrate. Avoidance is logged as signal, not shame.

8. Decision capture and resolution.
   Capture probability/date predictions, resolve them later, and surface calibration gently in weekly review.

9. Background repo/research dispatch.
   Start a bounded background job with a visible task row, status, cancel path, source list, and final report. Do not pollute the live chat session.

10. Capability/status recovery.
   "what can you do?", "what broke?", "why did you ping me?", "what are you waiting for?", and "what did you remember?" should work without reading logs.

## 6. Compound-turn architecture recommendation

Keep the existing `agents/compound_turn.py` as a useful prototype, but stop treating it as the final architecture. It currently:

- Uses `tools/dispatch/task_extractor.py` to detect compound messages via connective keywords and a minimum word count.
- Extracts task strings with a cheap aux model.
- Runs topological dependency waves.
- Executes each task by calling `run_internal_control`.
- Joins successful string results.
- Is invoked from both text and voice paths in `agents/telegram_bridge.py`.

That proves the shape, but it is still too untyped. The next version should introduce a durable WorkPacket model and typed task graph:

```text
WorkPacket
- packet_id
- source: text | voice | proactive | background
- raw_user_text or transcript event id
- status: queued | running | waiting_approval | partial | succeeded | failed | cancelled
- created_at, updated_at

WorkStep
- step_id
- packet_id
- phrase_span
- domain: memory | gmail | calendar | wiki | link | receipt | decision | reminder | weather | research | github | notion | drive | reply
- operation: read | summarize | draft | write | send | delete | schedule | log
- dependency_step_ids
- parallel_group
- risk_tier: safe | implicit | inline_confirm | typed_confirm | clarify
- tool_or_subagent
- expected_result_schema
- failure_policy
- status
- approval_id
- result_summary
- source_refs
```

Execution recommendation:

1. Parse the user turn into a typed task graph before executing tools.
2. Run safe read steps with typed adapters where possible instead of sending every subtask back through an LLM.
3. Use `run_internal_control` or specialist subagents for synthesis-heavy domains only.
4. Never run writes, sends, deletes, public posts, code execution, or GitHub/Notion/Drive/Gmail writes in a blind batch.
5. Convert risky steps into approval rows with exact payload previews.
6. Store packet/step status durably so `/work`, `/tasks`, `/audit`, and restart recovery can render it.
7. Generate the final visible reply from normalized step results, not concatenated subtask prose.

Anthropic's parallel-tool docs are the constraint to design around: same-turn tool calls are unordered, so only independent work should be batched; dependent calls need a later step or an error/result cycle ([Anthropic parallel tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use)).

## 7. Parallel tool execution recommendation

Hikari should support two kinds of parallelism:

1. Model-native parallel tool calls inside ordinary SDK turns.
2. Application-level parallel read batches inside WorkPackets.

Use application-level parallelism for product workflows. It lets Hikari control dependencies, timeouts, approval boundaries, partial failures, and the final receipt. Model-native parallelism can still help inside a single specialist turn, but it should not be the only plan.

Concrete rules:

- Parallelize independent reads: calendar + reminders + weather + important Gmail; recall + session search + wiki + link search; calendar event + Gmail thread + Drive docs + wiki notes.
- Cap fanout at 3 to 5 domains per Telegram turn unless the user explicitly asked for a background job.
- Add per-step timeouts and structured partial failures.
- Return all read results into one synthesis payload.
- Serialize writes to the same resource.
- Never batch approval creation with approval execution.
- Never parallelize two destructive operations or two writes to the same external object.
- Keep `_RUN_LOCK` for stateful visible turns only. Stateless internal reads should not mutate live SDK session state.

Add a semantic catalog before expanding parallel execution. `tools/router/tool_search.py` currently indexes mostly tool id tokens. MCP's tool model includes human title, description, input schema, output schema, and annotations ([MCP tools spec](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)); Hikari should generate comparable local metadata from `config/tools.yaml` plus tool manifests:

- human name
- domain tags
- operation type
- risk tier
- read/write/destructive/send
- freshness/currentness
- credential requirements
- examples of user phrasing
- result schema
- presentation recipe
- common failure modes

OpenClaw's tool-search docs make the same product point: large catalogs should be compactly searchable, policy-filtered, and telemetry-visible instead of fully exposed up front ([OpenClaw tool search](https://docs.openclaw.ai/tools/tool-search)).

## 8. Telegram/voice UX recommendation

Telegram is the product surface, not just transport. Hikari should use commands for global recovery, buttons for local decisions, typing/chat actions for short progress, and receipts for work completion.

Current strengths:

- Owner-gated Telegram bridge.
- Command menu and cockpit helpers.
- `/status`, `/tools`, `/audit`, `/settings`, `/approvals`, `/proactive`, `/reminders`, `/checkin`, `/capabilities`.
- Voice transcription path.
- Inline callbacks for approvals, reminders, and check-ins.
- Final-sent persistence after successful delivery.
- Throttled background progress.

Recommended WorkPacket UX:

1. Initial acknowledgement for multi-step work:

   ```text
   caught 4 things. doing the calendar, reminder, and link save now.
   the email stays as a draft until you confirm.
   ```

2. For short work, use `sendChatAction` instead of chat clutter. Telegram documents chat actions for cases where a bot response takes noticeable time ([Telegram Bot API](https://core.telegram.org/bots/api)).

3. For long work, send at most one progress message unless it becomes a background job.

4. Final receipt:

   ```text
   done
   1. reminder #143 created
   2. link #88 saved
   3. "Design sync" is still tomorrow at 10:00

   waiting
   4. Gmail draft #42 to Sam is ready
      type CONFIRM-SEND 42 to send
   ```

5. Use inline buttons for reversible/local choices: snooze, dismiss, mute source, why this, retry safe step, reject, edit, audit.

6. Use typed confirmation for high-risk work: email send/reply, public comments, calendar invites/deletes, Drive sharing/deletes, GitHub merges, Notion destructive writes, local execution, and code dispatch. NN/g warns that routine confirmation clicks become automatic, so high-risk approvals should require a non-routine action ([NN/g confirmation dialogs](https://www.nngroup.com/articles/confirmation-dialog/)).

7. Add `/work` or upgrade `/tasks` to show active/recent WorkPackets with packet id, status, waiting approvals, failures, and cancel/retry affordances.

8. Add `/receipt` for today's made/moved/learned/avoided slip and one-tap capture.

Telegram commands and inline keyboards are native Bot API concepts, including `setMyCommands` and `InlineKeyboardMarkup` ([Telegram Bot API](https://core.telegram.org/bots/api)). Use them, but keep normal work natural-language first.

## 9. Encouragement/proactivity recommendation

The product law from the encouragement report is correct:

> Hikari should only interrupt when she can name the anchor, name the value, and make the next move smaller.

Current repo state:

- `config/engagement.yaml` already has `priority_tier` and `min_interval_minutes` for sources.
- `agents/engagement/selector.py` scores novelty, actionability, confidence, time-of-day, mood, source response rate, recency, and priority tier.
- `agents/engagement/guard.py` blocks generic openers and requires anchor tokens for many sources.
- `agents/engagement/sender.py` already recognizes `[[defer:next_turn]]` and writes deferred items into `session_scratch`.
- `agents/proactive_gate.py` enforces quiet hours, silence windows, dedup, empty text rejection, and successful-send persistence.

Next recommendation: upgrade "priority tier" into explicit source policy:

```yaml
engagement:
  calendar_event_prep:
    send_mode: high_value
    interruption_right: immediate_if_within_window
    min_value_score: 0.65
  gmail_unread_threshold:
    send_mode: rare_opt_in
    interruption_right: digest_only
    min_value_score: 0.80
  weirdly_good_mood_leak:
    send_mode: silent_by_default
```

Priority policy:

- P0: exact reminders, explicit user-requested follow-ups, safety/weather alerts with real impact.
- P1: calendar prep, important email thread, due decision resolution, configured daily check-in, opted-in receipt ceremony.
- P2: active open loop, wiki/link/Drive/Notion changes tied to current work, callback to high-confidence memory.
- P3: unread threshold, re-engagement silence, location recurrence, readwise/review prompts.
- Silent by default: mood leak, generic good morning, "thinking of you", unanchored comfort, repeated no-response nudges.

Add a proactive value rubric:

- anchor strength
- user value
- actionability
- timing fit
- novelty
- emotional appropriateness
- privacy/sensitivity
- interruption cost
- feedback history

Supportive Accountability is useful because it frames accountability as effective when the supporter is experienced as trustworthy, benevolent, and competent ([Supportive Accountability](https://www.jmir.org/2011/1/e30/)). Microsoft HAI guidelines add the product controls: make clear what the system can do, make clear why it acted, support dismissal/correction, learn from user behavior, and provide global controls ([Microsoft HAI guidelines](https://www.microsoft.com/en-us/research/articles/guidelines-for-human-ai-interaction-eighteen-best-practices-for-human-centered-ai-design/)).

## 10. Tool surface recommendation

Do not add more tools first. Make the existing 169-tool surface feel like 8 to 10 dependable workflows.

User-facing capability groups:

- Today: calendar, reminders, weather, important mail, open loops.
- Capture: links, files, photos, voice notes, Apple Notes, wiki append, memory, receipt.
- Find: memory, session search, wiki, links, Drive, Codex reports.
- Act safely: Gmail drafts/sends, calendar writes, Docs/Drive/Sheets/Slides writes, Notion, GitHub, Apple.
- Reflect: receipt, decision log, weekly review, memory corrections.
- Research: web, official docs, arXiv, GitHub reads, YouTube transcript.
- Local/operator: status, jobs, approvals, audit, MCP health, backups.
- Delight: photo generation, stickers, music, mood leaks. Hide from primary capability story.

Immediate changes:

- Upgrade `/capabilities` from family counts to semantic workflow/risk view.
- Upgrade `/tools` from raw policy/recent/audit toward "what Hikari can do right now" with configured/unconfigured/gated/untrusted status.
- Keep `/audit` and `/approvals` as trust surfaces.
- Add tool labels: `safe read`, `untrusted read`, `personal data read`, `implicit write`, `asks first`, `external send`, `destructive`, `blocked`.
- Add presentation recipes for top tools: Gmail thread, calendar event, reminder, day receipt, link shelf, wiki result, weather, GitHub PR/issue, Drive file, Notion page.
- Fix stale skill docs: `.agents/skills/drive-search/SKILL.md` still references old `drive_search`, `drive_read_file`, `sheets_read`, and `gmail_search` names, while the current subagent prompt lists `drive_search_files`, `drive_read_file_content`, `sheets_read_range`, `query_gmail_emails`, and `gmail_get_message_details`.

Hermes' toolset model is the right inspiration: tools are organized into logical toolsets that can be enabled per platform, including common sets like web, terminal, browser, skills, memory, cronjob, delegation, and MCP-derived dynamic toolsets ([Hermes tools](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools)). OpenClaw's approvals model is the right safety inspiration: high-risk execution should bind exact cwd, argv, env, file operand, policy, allowlist, and user approval where applicable ([OpenClaw exec approvals](https://docs.openclaw.ai/tools/exec-approvals)).

## 11. Four implementation phases

### Phase 1: Trust and capability cleanup

Goal: make current machinery understandable and remove stale guidance.

Deliverables:

- Semantic tool catalog generated from `config/tools.yaml`.
- `/capabilities` grouped by workflow, risk, auth status, and common natural-language asks.
- `/tools` focused on policy and recent tool calls, not capability marketing.
- Updated `.agents/skills/drive-search/SKILL.md`.
- Approval previews audited for the top write families.
- Current compound-turn behavior documented as prototype.

Exit criteria:

- User can ask "what can you access right now?" and get accurate status.
- User can ask "what needs approval?" and see exact pending actions.
- Tool search returns useful hits for "email", "calendar", "receipt", "wiki", "weather", "youtube", "github", "notion".

### Phase 2: WorkPacket compound executor

Goal: make multi-intent text and voice messages reliable.

Deliverables:

- Durable `work_packets` and `work_packet_steps` or equivalent projection.
- Typed task graph replacing raw task-string execution.
- Parallel read executor with per-step timeout.
- Approval conversion for risky writes.
- Normalized result schema: done, waiting, failed, skipped, sources, next actions.
- `/work` read-only cockpit.

Exit criteria:

- A single message with 3 to 5 intents produces one packet and one final receipt.
- Independent reads run concurrently.
- Dependent writes wait.
- Partial failures are visible and do not erase successes.
- Final-sent persistence still belongs to the Telegram bridge.

### Phase 3: Telegram/voice and high-signal proactivity

Goal: make Hikari active where it matters and quiet where it does not.

Deliverables:

- VoiceTaskExtractor over transcripts.
- Risk-tiered voice checklist and receipt rendering.
- Telegram progress policy: typing/chat action, one progress message, final receipt.
- Proactive source send modes.
- Proactive value rubric.
- Formal deferred-noticing flow using `session_scratch`.
- `Why this?`, `Snooze`, and `Mute source` callbacks for proactives.

Exit criteria:

- Voice note with several tasks becomes a safe packet.
- Generic proactive messages fail evals.
- Exact reminders and calendar prep beat low-value sources.
- User can inspect why a proactive fired or was suppressed.

### Phase 4: Trusted workflow library and cockpit polish

Goal: make repeated usefulness natural.

Deliverables:

- Workflow bundles: morning command center, inbox triage, meeting prep, voice actions, capture-to-wiki/link/memory, day receipt, decision review, research packet, repo dispatch.
- Skill/workflow catalog with risk, tools, trigger phrases, and last used.
- Minimal local operator panel only after Telegram cockpit is solid.
- Weekly usefulness telemetry: approvals, dismissals, snoozes, completions, repeated workflow use.

Exit criteria:

- Workflows are invokable by natural language, not tool names.
- Operator can debug state without reading logs.
- Skill/workflow changes are inspectable and test-covered.

## 12. First 20 concrete build tasks

1. Add `tools/catalog.py` that reads `config/tools.yaml` and emits domain, operation type, risk label, auth requirement, untrusted-output flag, gate, examples, and presentation hint for every tool id.

2. Upgrade `tools/router/tool_search.py` to index catalog descriptions, tags, examples, and aliases instead of only tool id tokens. Add tests for "email", "calendar", "receipt", "wiki", "weather", "youtube", "github", "notion", and "approval".

3. Update `.agents/skills/drive-search/SKILL.md` to current OAuth wording and current Google Workspace tool names: `drive_search_files`, `drive_read_file_content`, `sheets_read_range`, `query_gmail_emails`, and `gmail_get_message_details`.

4. Upgrade `/capabilities` to show workflow groups, configured/unconfigured MCP servers, gated writes, untrusted reads, skills, and top natural-language examples.

5. Add `/work` or extend `/tasks` with a WorkPacket view: packet id, source, status, active step, waiting approval, retryable failures, cancel action, and final receipt link.

6. Expand `/approvals` rows to include operation, exact target, critical payload fields, reversibility, expiry, and audit id. Add snapshot tests for Gmail, calendar, Drive, GitHub, Notion, Apple, dispatch, and Python approvals.

7. Create a `WorkPacket` data model and persistence layer with `work_packets` and `work_packet_steps` tables or an equivalent durable projection.

8. Replace the compound task schema from `{"task": str, "depends_on": []}` with typed step objects containing domain, operation, dependencies, risk tier, expected output schema, and failure policy.

9. Add an executor that runs safe independent read steps with `asyncio.gather`, per-step timeouts, structured `StepResult` objects, and fanout caps.

10. Add typed read adapters for the first daily bundle: calendar read, reminders list, weather, important Gmail read, receipt today/yesterday, memory/tasks.

11. Add approval conversion for risky WorkPacket steps: Gmail send/reply/delete, calendar create/delete/invite, Drive/Docs/Sheets/Slides writes, Notion writes, GitHub writes, Apple writes, `dispatch_claude_session`, and write-enabled local execution.

12. Add a final receipt renderer that separates `done`, `waiting`, `failed`, `skipped`, and `sources`, and assert it sends at most one acknowledgement, one optional progress message, and one final receipt.

13. Route text compound turns in `agents/telegram_bridge.py` through WorkPacket creation while preserving raw user-message persistence and final-sent assistant persistence.

14. Route voice transcripts through the same planner, with phrase spans, low-confidence STT handling, and "clarify first" for ambiguous names/times or high-risk sends.

15. Add source `send_mode`, `min_value_score`, and `interruption_right` fields to `config/engagement.yaml` for every engagement source.

16. Extend `agents/engagement/selector.py` with a value rubric: anchor strength, user value, actionability, timing fit, novelty, emotional appropriateness, privacy/sensitivity, interruption cost, and feedback history.

17. Formalize deferred proactives: store `send_now`, `save_for_next_turn`, `save_for_reflection`, and `discard` outcomes, and surface deferred items in the next prompt injection only when contextually relevant.

18. Add proactive control callbacks: `pro:why:<id>`, `pro:snooze:<source>:<duration>`, and `pro:mute:<source>`, with owner-gated callback tests.

19. Add top workflow presentation recipes for Gmail thread summary, calendar event prep, reminder cards, receipt entries, link saves, wiki search/read, weather, GitHub PR/issue, Notion page, and Drive file.

20. Add compound-turn trajectory eval cases with 3 to 5 intents that assert required tool families, forbidden sends/deletes before approval, final receipt sections, partial-failure behavior, and no generic supportive filler.

## 13. What not to build

- Do not add more generic motivational copy. Unanchored encouragement is negative product value.
- Do not broaden to many chat channels yet. Telegram fidelity is the current moat.
- Do not build a public skill marketplace before scanning, source pinning, provenance, review, and rollback.
- Do not add more tools before making the current 169-tool surface usable.
- Do not increase `DEFAULT_MAX_TURNS` to hide compound-turn failures. Build orchestration.
- Do not expose `/exec`-style shell power as a normal user feature.
- Do not turn mood leaks, stickers, music, or photo generation into core productivity features.
- Do not build a local web dashboard before Telegram cockpit commands expose the same state.
- Do not let proactive unread-count checks masquerade as useful inbox triage.
- Do not let final replies include raw specialist output, raw tool JSON, or hidden reasoning.
- Do not weaken final-sent persistence for progress messages or ephemeral drafts.
- Do not auto-send, auto-delete, auto-publish, auto-merge, or auto-share from inferred intent.

## 14. Tests/evals to add

1. Compound planner unit tests.
   Assert task splitting, dependency detection, domain classification, operation classification, risk tiers, fanout caps, and clarification for ambiguous recipients/times.

2. WorkPacket persistence tests.
   Assert packet/step creation, status transitions, restart recovery, cancellation, approval linkage, and final receipt rendering.

3. Parallel executor tests.
   Assert independent reads run concurrently, dependent writes wait, same-resource writes serialize, timeouts become structured failures, and successful steps survive partial failure.

4. Runtime isolation tests.
   Assert WorkPacket internal reads do not update `session_id`, visible turns still serialize through `_RUN_LOCK`, and final-sent persistence remains bridge-owned.

5. Voice workflow tests.
   Assert a four-task transcript creates four ordered steps; risky actions stay pending; ambiguous STT asks one clarification; no-action voice notes remain conversational.

6. Telegram spam-control tests.
   Assert a multi-tool packet sends at most one acknowledgement, optional one progress message, and one receipt; progress/draft text is not persisted as final assistant text.

7. Approval matrix tests.
   For every external write family, assert no execution before approval and preview includes exact target, payload/diff/body/count, reversibility, service, and expiry.

8. Approval drift tests.
   If recipient, file path, command, branch, event time, body, cwd, or payload changes after approval, execution is rejected. This follows the same binding principle as OpenClaw exec approvals ([OpenClaw exec approvals](https://docs.openclaw.ai/tools/exec-approvals)).

9. Tool truthfulness evals.
   Gmail claims require Gmail read/subagent use; calendar claims require calendar use; weather claims require weather use; place-hours claims require places/open-now; receipt claims require receipt tool; saved-link claims require link shelf; memory claims require recall or injected context.

10. Prompt-injection red-team tests.
    Malicious Gmail, Docs, Drive files, PDFs, webpages, calendar descriptions, GitHub issues, and YouTube transcripts must not trigger writes, exfiltrate secrets, or override Hikari's instructions. OWASP flags prompt injection and excessive agency as core LLM application risks ([OWASP LLM01](https://genai.owasp.org/llmrisk/llm01-prompt-injection/), [OWASP LLM06](https://genai.owasp.org/llmrisk/llm062025-excessive-agency/)).

11. Proactive priority tests.
    Reminder beats Gmail threshold; calendar prep beats mood leak; safety/weather beats re-engagement; dedup suppresses repeats; quiet hours suppress non-urgent sources.

12. Proactive value-rubric evals.
    Every sent proactive has anchor, value, action, timing reason, source, dedup key, and control path. Weak candidates become `save_for_next_turn` or `NO_MESSAGE`.

13. Anti-generic voice evals.
    Fail "you got this", "just checking in", "hope you're doing well", "i'm proud of you" without evidence, "i'm always here", and task-solicitation endings.

14. Receipt response tests.
    `made` celebrates concrete output; `moved` marks progress without inflation; `learned` becomes a heuristic; `avoided` shrinks or inspects without shame.

15. Degraded-auth tests.
    Missing Google/Notion/GitHub/DeepL/OpenAI STT auth should produce literal, bounded failures and continue with local context when possible.

16. Tool catalog tests.
    Every registry tool has domain, risk label, operation type, auth status, untrusted-output label, and a presentation policy.

17. Capability UX tests.
    "what can you do?" should show task-oriented workflows, not raw tool schemas or toy features.

18. Feedback learning tests.
    Thumbs-down, `/silence`, no-response, snooze, and mute reduce future source priority; replies/completions raise it cautiously.

## 15. Risks and sequencing notes

The first risk is mistaking scaffolding for product. `agents/compound_turn.py`, `/capabilities`, `priority_tier`, and deferred proactive scratch are real starts, but they need typed contracts, durable state, and evals before they can carry daily workflows.

The second risk is proactivity creep. Hikari already has many producers. Add source policy, value scoring, and stay-silent evals before enabling more sources.

The third risk is stale tool guidance. The drive-search skill is already behind current Google Workspace names. A stale skill can make Hikari less useful than if she had no skill at all.

The fourth risk is bypassing safety for speed. Compound execution must route writes through gatekeeper/approvals, preserve untrusted-output wrapping, and keep final-sent persistence. External docs and OWASP both point to the same danger: tool-capable agents fail badly when agency exceeds policy.

The fifth risk is making the user operate the agent. Capability visibility is for trust and recovery, not making the user choose tools. Natural language and voice should remain the main path.

Recommended build order:

1. Fix stale docs and tool catalog metadata.
2. Upgrade `/capabilities`, `/approvals`, and `/work` surfaces.
3. Promote compound-turn prototype into typed WorkPackets.
4. Add parallel read executor and approval conversion.
5. Add Telegram/voice receipt rendering.
6. Add proactive send modes and value rubric.
7. Add compound/proactive/tool-truth eval suites.
8. Package the top workflows as trusted Hikari workflows.

Hikari should feel like: the person in your pocket who remembers the thread, checks the real world, does the boring parallel work before you ask twice, asks before touching anything dangerous, stays quiet when quiet is the useful move, and hands you the next small step with enough attitude to feel alive.
