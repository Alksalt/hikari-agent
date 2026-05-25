# Research Synthesis: Useful, Active Bot Roadmap for Hikari Agent

Date: 2026-05-25
Repo: `/Users/ol/agents/hikari-agent`

## 1. Executive summary

Hikari's product direction is not "a chatbot with personality" and not "a generic autonomous agent platform." The useful product is a single-owner Telegram agent that can remember, inspect the user's real systems, take bounded action, execute compound asks, and interrupt only when she has a concrete reason. The differentiator is the combination of intimacy and operational discipline: Hikari should feel personal because she knows the user, and trustworthy because she can show where her claims came from, what tool she used, what she is doing next, and what she will not do without approval.

The repo already has a strong base: a three-way runtime split in `agents/runtime.py`, strict final-sent persistence through the Telegram bridge, a central tool registry in `config/tools.yaml`, durable approval gates, untrusted-output wrapping, direct utility tools, subagent prompts, memory/provenance tables, proactive engagement candidates, cadence caps, receipt/reminder/decision systems, voice transcription, and broad tests around these invariants.

The gap is product execution. Today, Hikari can be useful in isolated turns, but the architecture still leans on the model's live session to notice, plan, and sequence too much. Compound requests like "check my calendar, summarize urgent mail, draft a reply, remind me tonight, and add the decision to my receipt" need a deterministic task graph and parallel read executor. Proactive behavior needs a source-priority policy that prefers exact reminders, calendar prep, due decisions, and user-anchored callbacks over mood leaks or generic check-ins. Tool UX needs a capability map and status cockpit so the user can ask, "what can you access?", "what are you doing?", "what needs approval?", and "why did you ping me?"

External research supports this direction. Anthropic's tool docs say independent tool calls can be issued in one assistant turn and executed concurrently, while dependent calls belong in separate turns ([Anthropic parallel tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use)). Anthropic's subagent docs frame specialists as separate context windows with restricted tools and independent permissions ([Claude Code subagents](https://code.claude.com/docs/en/sub-agents)). MCP defines tools through names, descriptions, input schemas, output schemas, and annotations, which maps directly to Hikari's registry and missing semantic catalog layer ([MCP tools spec](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)). Telegram exposes native surfaces for typing/progress, voice notes, inline keyboards, and now ephemeral message drafts, so Hikari should use Telegram-native affordances instead of chat clutter ([Telegram Bot API](https://core.telegram.org/bots/api)). Human-AI guidance also points toward contextual timing, user control, feedback, and clear capability boundaries ([Microsoft HAI guidelines](https://www.microsoft.com/en-us/research/articles/guidelines-for-human-ai-interaction-eighteen-best-practices-for-human-centered-ai-design/)).

Roadmap thesis: build compound-turn execution, capability/tool UX, and high-signal proactivity before adding more tools, more persona flourish, more channels, or broader autonomous behavior.

## 2. Research inputs reviewed

Available research reports read:

- `codex/research-active-encouragement-ux-2026-05-25.md`
- `codex/research-hermes-openclaw-competitive-2026-05-25.md`

Requested reports not present in `codex/` at synthesis time:

- `codex/research-usefulness-non-generic-bot-2026-05-25.md`
- `codex/research-compound-turn-parallel-tools-2026-05-25.md`
- `codex/research-tool-usefulness-audit-2026-05-25.md`
- `codex/research-telegram-voice-ux-2026-05-25.md`

Local repo inputs inspected:

- `README.md`, `CLAUDE.md`, `AGENTS.md`
- `agents/runtime.py`, `agents/hooks.py`, `agents/tool_inventory.py`, `agents/telegram_bridge.py`, `agents/messaging.py`, `agents/post_filter.py`
- `agents/scheduler.py`, `agents/proactive.py`, `agents/engagement/`, `agents/daily_checkin.py`, `agents/cadence.py`
- `tools/README.md`, `tools/_tools_yaml.py`, `tools/_registry.py`, `tools/_response.py`, `tools/gatekeeper.py`, `tools/approvals.py`, `tools/router/tool_search.py`, `tools/dispatch/session.py`, direct utility tools
- `config/tools.yaml`, `config/engagement.yaml`
- `.agents/skills/character-voice`, `.agents/skills/recall-memory`, `.agents/skills/drive-search`, `.agents/skills/generate-photo`, `.agents/skills/schedule-heartbeat`
- `agents/subagents/prompts/`
- relevant tests in `tests/` and `evals/conversation/`

External sources used:

- Anthropic parallel tool use: [Parallel tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use)
- Anthropic subagent docs: [Create custom subagents](https://code.claude.com/docs/en/sub-agents)
- MCP tool schema and annotations: [MCP server/tools spec](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)
- OpenAI Agents SDK tools and handoffs, for comparison: [Tools](https://openai.github.io/openai-agents-python/tools/) and [Handoffs](https://openai.github.io/openai-agents-python/handoffs/)
- Telegram Bot API: [Bot API](https://core.telegram.org/bots/api)
- Human-AI interaction guidance: [Microsoft HAI guidelines](https://www.microsoft.com/en-us/research/articles/guidelines-for-human-ai-interaction-eighteen-best-practices-for-human-centered-ai-design/)
- Behavior-change/support models: [Fogg Behavior Model](https://www.behaviormodel.org/) and [Supportive Accountability](https://www.jmir.org/2011/1/e30/)
- Competitive references: [Hermes Agent GitHub](https://github.com/nousresearch/hermes-agent), [Hermes tools docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools/), [OpenClaw GitHub](https://github.com/openclaw/openclaw), [OpenClaw site](https://openclaw.ai/)

## 3. Five product principles

1. Specific beats supportive. Hikari should not say "you got this" when she can say which file, email, event, task, reminder, receipt, or decision is active. Every useful message should include an anchor.

2. Tools are part of the personality. Hikari becomes non-generic when she can inspect the user's real calendar, mail, wiki, repo, reminders, receipts, links, and memory, then speak from that evidence. A tool call should not make her sound like a dashboard; it should make her more concrete.

3. Compound tasks need an executor, not vibes. Multi-intent user messages should become a task graph with dependencies, parallel-safe reads, gated writes, partial failures, and a final synthesized reply.

4. Proactivity must earn the interruption. Exact reminders, calendar prep, due decisions, and user-requested follow-ups deserve priority. Generic re-engagement, unread-count nags, mood leaks, and "thinking of you" messages should be rare, opt-in, or silent.

5. Trust requires inspectability. The user needs simple surfaces for capabilities, tool health, approvals, background jobs, memory, proactive sources, and recent failures. Hermes and OpenClaw make capabilities visible; Hikari should do the same without becoming a generic control-panel product.

## 4. Desired Hikari behavior statement

When Hikari works, she reads one user message as a whole, splits it into concrete work, checks the right sources without being asked when the answer depends on current or owned data, runs independent reads in parallel, asks for approval before risky writes, and returns a compact answer that names what she did and what remains.

She should sound like one person, not a committee of tools. The user should feel that she noticed the right thing, held the thread, reduced the next action, and did not make the user manage the agent.

Desired turn shape:

1. Understand the full ask, including implicit subtasks.
2. Use memory or tools when the answer depends on user-specific, current, or external facts.
3. Execute independent read work concurrently.
4. Gate sends, deletes, uploads, calendar writes, and other irreversible operations with clear previews.
5. Return a specific result with sources, IDs, next action, and unresolved items.
6. Persist only what was actually sent or actually resolved.

Desired proactive shape:

1. Only send when the candidate has an anchor, user value, timing fit, and actionability.
2. Prefer "do this one smaller thing" over broad motivation.
3. Explain itself in the message through the anchor, not a verbose audit paragraph.
4. Leave an inspectable event record.

## 5. Highest-leverage user workflows

1. Morning command center. One request or scheduled ceremony that checks calendar, reminders, weather, decision deadlines, top inbox risks, and open loops. Output should be a short prioritized brief plus action chips: summarize thread, draft reply, snooze reminder, mark decision resolved, log receipt.

2. Compound personal admin. "Check whether I owe anyone a reply, draft the one that matters, remind me tonight, and add it to the receipt." This should read Gmail/calendar/tasks in parallel, draft but not send email, create reminders only after approval when needed, and log the result.

3. Calendar/event prep. Before a meeting or shift, Hikari should fetch event details, related notes/wiki/email if available, produce a two-minute prep card, and optionally create a follow-up task after the event.

4. Inbox triage. Not a generic unread count. Hikari should identify urgent or live threads, summarize the one thread that matters, draft replies, flag attachment/action needs, and leave low-value mail alone.

5. Voice note to actions. The user sends a voice note; Hikari transcribes it, extracts reminders/tasks/decisions/receipt entries, asks for approval on writes, and replies with a compact action summary. Telegram already represents voice notes with file IDs, duration, MIME type, and file size in the Bot API, and Hikari already has Whisper transcription wired through `tools/voice.py` and `agents/telegram_bridge.py`.

6. Personal knowledge capture and retrieval. URLs should enter the link shelf; durable ideas should go to wiki; repeated phrases should update lexicon/memory; Hikari should later resurface them only when contextually adjacent.

7. Project/repo handoff. Telegram ask starts a `dispatch_claude_session` for repo research or code work, with progress pings, a visible job ID, a final summary, and no live-session pollution.

8. Decision follow-through. Hikari should record forecasts/decisions, ask when due, compute outcomes, and use Brier-style feedback as a weekly learning loop.

9. Day receipt recovery. "Log that I avoided the migration again" should not become shame. It should create a receipt entry and optionally propose a smaller next action.

10. Capability/status recovery. The user should be able to ask "what can you do right now?", "what broke?", "what are you waiting for?", and "why did you ping me?" and get accurate answers from the registry, scheduler, approvals, and proactive event logs.

## 6. Compound-turn architecture recommendation

Add a deterministic compound-turn orchestrator in front of normal visible response generation. Do not rely only on the model's four-turn live SDK loop to discover and complete every subtask.

Recommended architecture:

1. `compound_turn.detect(user_text, context)` classifies whether the message has multiple intents, multiple domains, or read/write dependencies.

2. `compound_turn.plan(...)` returns a task graph:
   - `task_id`
   - domain: `memory`, `gmail`, `calendar`, `wiki`, `repo`, `reminder`, `receipt`, `research`, `notion`, `drive`, `weather`, `reply`
   - operation: read, synthesize, draft, write, send, delete, schedule
   - dependencies
   - approval class
   - can_parallel
   - expected output contract
   - failure policy

3. Independent read tasks run through a parallel executor. Use typed adapters where they exist (`tools/calendar`, reminders, weather, memory, receipt, link shelf), and use `run_internal_control` or subagent prompts for domains that need an LLM wrapper (`drive_gmail`, `wiki`, `research`, `github`, `notion`). Internal control calls are stateless, do not resume the live SDK session, and do not acquire `_RUN_LOCK`, which matches the `AGENTS.md` contract.

4. Risky writes are converted into approval requests or draft artifacts. The planner should not ask the model to manage approval state ad hoc. Existing `tools/gatekeeper.py` and `tools/approvals.py` should remain the write boundary.

5. The final visible reply is generated once, after the orchestrator has normalized results into a compact context block. This can be done by calling the visible runtime with the original user text plus a structured "completed work" payload, or by composing in an internal control pass and sending through the Telegram bridge. Preserve the invariant that the Telegram bridge persists only final-sent text.

6. Store task graph and statuses in a durable table or projection over `background_tasks`, with fields for `turn_id`, `task_id`, `status`, `tool_names`, `approval_id`, `result_summary`, `error`, and `source_refs`.

Key rule: if a subtask depends on another subtask's result, do not batch it. Anthropic's parallel tool docs explicitly state that same-turn tool calls are unordered and should be independent; dependent calls belong in later turns or after an error/result cycle ([Anthropic parallel tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use)).

Concrete examples:

- "What's tomorrow look like, and remind me to bring the keys" becomes `calendar_read` plus `weather_read` in parallel, then `reminder_create` gated or direct depending on reminder policy.
- "Check if Sarah emailed, draft a reply, and put the call on my calendar" becomes `gmail_search` -> `gmail_details` -> `draft_reply` -> approval for draft/calendar write.
- "Read this PDF, summarize it, save the source, and remind me Friday" becomes `read_attachment` -> `summary` plus `link_save`/wiki task plus reminder creation.

## 7. Parallel tool execution recommendation

Hikari should support parallel execution in two layers:

1. Model-native parallel tool calls for simple independent tools inside a single SDK turn.
2. Application-level parallel read batches for compound turns, independent of whether the model chooses to batch.

Use application-level parallelism first for reliability. It gives Hikari control over dependencies, timeouts, partial failures, approvals, and result normalization.

Implementation details:

- Add `tools/parallel_reads.py` or `agents/compound/executor.py` with `asyncio.gather` over independent read tasks.
- Cap total parallel fanout per turn, probably 3 to 5 domains for Telegram UX.
- Add per-domain timeout and partial-failure behavior: "calendar timed out, but inbox and reminders worked."
- Group all result summaries into one final synthesis payload so the visible response does not look like a tool transcript.
- Never run writes/deletes/sends in the same blind batch as reads.
- Never parallelize two writes to the same external resource.
- Keep `_RUN_LOCK` only around stateful visible turns. Parallel read/internal-control calls should not mutate live session state.

Registry improvement:

`tools/router/tool_search.py` currently builds BM25 search mostly from tool ID tokens. That is too thin for non-generic tool use. Add semantic metadata to `config/tools.yaml` or a generated catalog:

- human name
- domain tags
- operation type
- read/write/destructive/send
- freshness/currentness
- required credentials
- presentation recipe
- examples of user phrasing
- result schema
- common failure modes

This aligns with MCP's formal tool shape: name, title, description, input schema, output schema, and behavior annotations ([MCP tools spec](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)). It also mirrors OpenAI Agents SDK guidance around hosted tool search and deferred large tool surfaces ([OpenAI Agents SDK tools](https://openai.github.io/openai-agents-python/tools/)).

Local bug to fix early:

- `agents/engagement/producers/gmail_unread_threshold.py` and `agents/engagement/producers/gmail_important_thread.py` use `_MCP_SERVER = "claude_ai_Gmail"`, while the registry server is `google_workspace`. This likely suppresses warm-state Gmail proactive triggers.

## 8. Telegram/voice UX recommendation

Telegram is not just a transport; it is the product surface. Build for the chat thread, not around it.

Keep:

- owner-gated single-user Telegram posture
- typing heartbeat and false-start choreography in `agents/bridge_ux.py`
- inline approvals with exact confirm/reject semantics
- post-filter and final-sent persistence
- voice note transcription path
- reaction feedback and reaction-as-turn controls

Add:

1. Progress without clutter. For work expected to exceed 8 to 12 seconds, send a short progress status or use Telegram-native `sendChatAction`. Telegram recommends chat actions when bot responses take noticeable time ([Telegram Bot API](https://core.telegram.org/bots/api)). The newer `sendMessageDraft` method can stream an ephemeral 30-second draft and must be finalized with `sendMessage`; evaluate it for long compound turns before adopting because it affects persistence semantics.

2. Inline action keyboards for approval and small decisions. Telegram supports inline keyboard markup and callback data in bot messages ([Telegram Bot API](https://core.telegram.org/bots/api)). Hikari already has callbacks; extend them to common compound outputs: approve draft, snooze, mark done, drop, summarize thread, save to wiki.

3. Voice note action extraction. After transcription, run the same compound planner over the transcript. Output should include transcript confidence/errors, extracted actions, pending approvals, and "ignored as rambling" when no action is found.

4. One-message compound summaries. Avoid sending a separate chat message per tool result. The final answer should be a compact report with sections only when needed: done, needs approval, failed, next.

5. `/status` as an operator cockpit. The current bridge has status and cockpit commands; expand them into capability, jobs, approvals, proactive, memory, and health views instead of adding a web dashboard first.

Avoid:

- Streaming every thought.
- Turning progress into fake intimacy.
- Persisting assistant text before delivery.
- Making the user choose tools manually for ordinary requests.

## 9. Encouragement/proactivity recommendation

The rule from the active encouragement report is the right product law:

Hikari should only interrupt when she can name the anchor, name the value, and make the next move smaller.

Priority policy:

- Priority 0: exact reminders, user-requested follow-ups, safety/weather alerts with real impact, explicit calendar prep opt-ins.
- Priority 1: calendar event prep, important email thread, due decision resolution, daily check-in if configured, end-of-day receipt ceremony if opted in.
- Priority 2: wiki new file, open-loop follow-up, callback episode, starred Drive or Notion edit tied to active work.
- Priority 3: silence re-engagement, Gmail unread threshold, mood leak, location prompt, daily review style prompts.
- Silent by default: generic good mornings, generic check-ins, unanchored emotional support, repeated open-loop nudges, "thinking of you" presence pings.

Behavior-change research supports this: prompts work when motivation, ability, and prompt converge, not when the system merely wants engagement ([Fogg Behavior Model](https://www.behaviormodel.org/)). Supportive accountability works when accountability feels benevolent, competent, and aligned with clear expectations, which maps well to Hikari's "care through logistics" voice ([Supportive Accountability](https://www.jmir.org/2011/1/e30/)).

Implementation:

- Add explicit per-source priority and send mode to `config/engagement.yaml`.
- Add a proactive value rubric to `agents/engagement/selector.py`: anchor strength, user value, actionability, timing, novelty, emotional appropriateness, interruption cost.
- Add "save for next turn" and "save for reflection only" outcomes, not just send/discard.
- Promote receipt entries as the main encouragement substrate: `made`, `moved`, `learned`, `avoided`.
- Keep exact reminders exact. Do not rewrite user-authored reminder text through persona.
- Add an inspectable "why did this fire?" record for every proactive event.

Use Microsoft HAI's guidance here: AI systems should time services based on context, show relevant information, support efficient dismissal/correction, learn from user behavior, and provide global controls ([Microsoft HAI guidelines](https://www.microsoft.com/en-us/research/articles/guidelines-for-human-ai-interaction-eighteen-best-practices-for-human-centered-ai-design/)).

## 10. Tool surface recommendation

The tool surface should make Hikari meaningfully better than generic ChatGPT in seven ways:

1. She knows what is live. The injected `# tools available` block is a good internal start. Add a user-facing `/capabilities` view sourced from `config/tools.yaml`, MCP health, and skill metadata.

2. She uses tools when facts are external, current, or owned by the user. The post-filter already guards inbox/calendar fabrication through `LAST_TURN_TOOL_NAMES`; expand this into evals and planner rules.

3. She returns provenance. Tool outputs should include IDs, titles, timestamps, sources, and presentation hints. `tools/_response.py` already has an envelope; make the key tools use it consistently.

4. She drafts before sending. Gmail, calendar, Drive, Notion, GitHub, Apple, and external writes should present clear previews and approval state.

5. She can explain risk. Add tool labels: `safe read`, `untrusted read`, `personal data read`, `asks first`, `external send`, `destructive`, `blocked`.

6. She keeps user-owned state. Link shelf, receipts, decisions, reminders, memory, wiki, and open tasks are what make her non-generic.

7. She has a recovery cockpit. `/status`, `/approvals`, `/jobs`, `/memory`, `/proactive`, `/capabilities`, and `/doctor` should be accurate enough to debug without reading logs.

Competitive lesson:

- Hermes and OpenClaw make commands, tools, skills, background work, memory, and platform health visible. Hermes also exposes broad toolsets, memory, delegation, cron, and messaging surfaces ([Hermes Agent GitHub](https://github.com/nousresearch/hermes-agent), [Hermes tools docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools/)). OpenClaw's public positioning emphasizes a personal assistant across chat channels and many integrations ([OpenClaw GitHub](https://github.com/openclaw/openclaw), [OpenClaw site](https://openclaw.ai/)). Hikari should copy inspectability and workflow clarity, not broad platform sprawl.

## 11. Four implementation phases

### Phase 1: Make current capability visible and reliable

Goal: turn hidden machinery into user-facing trust.

Build `/capabilities`, `/jobs`, `/approvals`, `/proactive why`, and better `/status`. Fix stale skill docs and Gmail producer server IDs. Add presentation recipes for the most valuable tools.

Exit criteria:

- User can ask what Hikari can access and get accurate enabled/unconfigured/gated status.
- User can see pending/recent approvals and revoke persistent grants.
- User can see background/proactive/reminder job state.
- No known stale skill points at removed Google tool names.

### Phase 2: Compound-turn planner and parallel read executor

Goal: make multi-intent messages work without the user decomposing them.

Build task graph parsing, dependency classification, parallel read execution, result normalization, gated write handling, and final synthesis.

Exit criteria:

- A single Telegram message with at least three independent read tasks returns one synthesized result.
- Dependent writes wait for read results and approval.
- Partial failures are reported without losing successful work.
- Live SDK session is not polluted by internal read fanout.

### Phase 3: High-signal proactive and voice workflows

Goal: make Hikari active only where it matters.

Add source priority, proactive value rubric, save-for-next-turn outcome, voice-note action extraction, event prep cards, inbox triage, decision follow-up, and receipt-based encouragement.

Exit criteria:

- Exact reminders and calendar prep beat low-value sources.
- Generic proactive messages fail evals.
- Voice note can become tasks/reminders/decisions with approval.
- The user can inspect why the last proactive message fired or skipped.

### Phase 4: Workflow library and operator cockpit

Goal: make repeatable usefulness feel natural.

Create trusted workflow bundles for morning command center, inbox triage, calendar prep, voice note actions, research-to-wiki, repo dispatch, decision review, and day receipt. Add a minimal local operator panel only after Telegram cockpit commands are solid.

Exit criteria:

- Workflows are documented, testable, and invokable by natural language.
- Skill/workflow catalog shows risk, tools, trigger phrases, and last used.
- Operator panel mirrors Telegram status, not a separate product direction.

## 12. First 20 concrete build tasks

1. Fix Gmail proactive warm-server IDs: change both Gmail engagement producers from `claude_ai_Gmail` to `google_workspace`; add tests that warm `google_workspace` and assert Gmail candidates can appear.

2. Update or delete `.agents/skills/drive-search`: replace stale `drive_search`/`gmail_search` naming and service-account language with current `google_workspace` OAuth and tool names from `agents/subagents/prompts/drive_gmail.md`.

3. Add `tool_catalog` metadata generation from `config/tools.yaml`: domain, operation, risk label, credentials, examples, output shape, presentation hint.

4. Upgrade `tools/router/tool_search.py` to index semantic descriptions/tags/examples, not just tool ID tokens; add query tests for "email", "calendar", "docs", "receipt", "youtube", "weather", "wiki".

5. Add `/capabilities` command: render configured/unconfigured MCP servers, utility tools, gated writes, untrusted reads, skills, and common workflows.

6. Extend `/status` or add `/doctor`: include LaunchAgent/runtime health, DB path/migration status, scheduler running, MCP warm pool, OAuth health, recent errors, and model config.

7. Add `/jobs`: project scheduler jobs, background dispatch tasks, media outbox, graph outbox, reminders, proactive candidates/events, and failed internal jobs into one readable activity ledger.

8. Expand `/approvals`: list pending approvals, expired approvals, recent approvals/denials, tool name, critical fields, created/expiry time, and revoke action for persistent grants if present.

9. Add `agents/compound/planner.py`: deterministic parser returning a task graph for common domains before any tool execution.

10. Add `agents/compound/executor.py`: run independent read tasks with `asyncio.gather`, per-task timeouts, partial-failure result objects, and no live-session mutation.

11. Add compound result schema: `completed`, `needs_approval`, `failed`, `skipped`, `sources`, `next_actions`; use it for final synthesis.

12. Add approval conversion for compound writes: calendar create/delete, Gmail draft/send/reply/delete, Drive writes/deletes, Notion writes, GitHub writes, Apple writes, reminders if policy requires confirmation.

13. Add compound-turn trajectory cases under `evals/conversation/cases/layer_c/trajectory/` and update `discover_cases()` to include trajectory directories; existing trajectory harness already supports tool-call assertions but discovery omits trajectory files.

14. Add fake-MCP integration tests for a compound ask: Gmail read + calendar read + weather read + reminder create + final synthesis. Assert read tasks execute independently and write waits.

15. Add source priority to `config/engagement.yaml` for every engagement source: `must_send`, `high_value`, `contextual`, `rare_opt_in`, `silent_by_default`.

16. Add proactive value rubric in `agents/engagement/selector.py`: anchor strength, user value, actionability, timing, novelty, emotional appropriateness, interruption cost.

17. Add "save_for_next_turn" and "save_for_reflection" outcomes to engagement selection; test that weak but relevant noticings are stored, not sent.

18. Add voice-note action extraction: after transcription, pass transcript to compound planner; generate pending task/reminder/decision/receipt actions; ask approval for writes.

19. Add Telegram long-turn progress policy: after 8 to 12 seconds, send one concise progress event or `sendChatAction`; evaluate `sendMessageDraft` separately because it is ephemeral and must not break final-sent persistence.

20. Add presentation recipes for top tools: Gmail thread summary, calendar event prep, reminder list/create, day receipt, link shelf, wiki search/read, weather, GitHub PR/issue, Notion page, Drive file. Each recipe should define fields to show, fields to hide, source IDs, and failure phrasing.

## 13. What not to build

- Do not add more generic motivational copy. The active encouragement report is clear: unanchored praise and "just checking in" are negative product value.

- Do not broaden to many chat channels yet. Hikari's advantage is Telegram fidelity, voice, memory, and one-owner continuity.

- Do not copy public skill marketplace behavior before scanner, pinning, provenance, approval, and rollback exist.

- Do not increase `DEFAULT_MAX_TURNS` to paper over compound-turn failures. The repo deliberately pins chat max turns to 4; build orchestration instead.

- Do not expose broad shell or `/exec` style power as a normal user feature.

- Do not make proactive mood leaks a core feature. Keep them rare or off until high-value sources are excellent.

- Do not resurrect Readwise daily review until there is a real source/tool again; the current producer is effectively a stub.

- Do not let user-authored reminders be rewritten through persona by default.

- Do not build a local web dashboard before Telegram cockpit commands cover the same state.

- Do not make the user select tools manually for normal workflows. Capability visibility is for trust and recovery, not pushing orchestration onto the user.

## 14. Tests/evals to add

1. Compound planner unit tests:
   - splits multi-intent messages into tasks
   - identifies dependencies
   - classifies read/write/send/destructive operations
   - caps fanout and asks a clarification only for genuinely ambiguous work

2. Parallel executor tests:
   - independent reads run concurrently
   - dependent writes wait
   - same-resource writes serialize
   - partial failures preserve successful results
   - per-task timeout produces a user-readable failure

3. Runtime isolation tests:
   - compound internal reads do not acquire `_RUN_LOCK`
   - visible turns still serialize through `_RUN_LOCK`
   - internal control calls do not update `session_id`
   - final-sent persistence still lives in the Telegram bridge

4. Tool-use evals:
   - inbox claims require Gmail tool/subagent use
   - calendar claims require calendar tool/subagent use
   - external current facts require research/web citations
   - memory claims require recall or injected memory
   - untrusted content cannot trigger writes

5. Trajectory corpus:
   - add `trajectory/` discovery to Layer C
   - include compound asks with 3 to 5 intents
   - assert required tool families, forbidden tools, final text, and approval behavior

6. Proactive priority tests:
   - reminder beats Gmail threshold
   - calendar prep beats mood leak
   - safety/weather beats re-engagement
   - same dedup key suppresses repeat
   - quiet hours suppress non-urgent sources

7. Anti-generic proactive evals:
   - fail "you got this"
   - fail "just checking in"
   - fail unanchored "proud of you"
   - require anchor tokens
   - reward `NO_MESSAGE`

8. Voice workflow tests:
   - voice note under duration cap transcribes
   - transcript action extraction creates proposed reminders/tasks/decisions
   - write actions require approval
   - no-action voice notes get a normal conversational reply, not fake tasks

9. Tool catalog tests:
   - every registry tool has risk label and domain
   - every gated write has critical fields
   - every untrusted read is wrapped
   - no stale skill refers to removed tool names

10. Telegram UX tests:
   - long-turn progress sends at most one progress message per turn
   - inline callbacks resolve the correct task/approval
   - persisted assistant message matches final delivered text
   - progress/drafts are not persisted as final replies

## 15. Risks and sequencing notes

The main sequencing risk is building more surface area before the compound executor exists. More tools without orchestration will make Hikari look capable but behave inconsistently on realistic requests.

The second risk is proactivity creep. Hikari already has many producers and ceremonies. Add priority and "stay silent" evals before enabling more sources. Notification fatigue is a product failure, even when each individual message sounds clever.

The third risk is stale documentation and stale skills. The `drive-search` skill already appears behind the actual Google Workspace tool names; stale tool guidance directly undermines non-generic usefulness.

The fourth risk is safety regression through convenience. The current gatekeeper, untrusted wrappers, owner gate, and post-filter are competitive advantages. Compound execution must preserve those boundaries rather than bypassing them for speed.

The fifth risk is turning Hikari into a generic agent OS. Hermes and OpenClaw show the value of visible capabilities, jobs, skills, and policy, but Hikari should absorb those ideas in a one-owner, Telegram-first, voice-consistent way.

Recommended build order:

1. Fix stale/mismatched surfaces.
2. Make capabilities/jobs/approvals visible.
3. Build compound planner and parallel read executor.
4. Add compound-turn evals.
5. Add priority-based proactive selection.
6. Add voice-note action extraction.
7. Package repeatable workflows as trusted Hikari workflows.

Hikari should feel like: the person in your pocket who remembers the thread, checks the real world, does the boring parallel work before you ask twice, asks before touching anything dangerous, and then hands you the next small move with just enough attitude to make it feel alive.
