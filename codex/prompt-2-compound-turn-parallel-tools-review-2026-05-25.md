# Prompt 2 — Compound Turn / Parallel Tools Review

Date: 2026-05-25  
Repo: `/Users/ol/agents/hikari-agent`  
Scope: research/design only; no source-code changes.

## 1. Executive summary

Hikari already has a prototype compound-turn path: Telegram text and voice handlers detect likely compound input, call `tools.dispatch.task_extractor.extract_tasks()`, and run `agents.compound_turn.run_compound_turn()` when more than one task is returned. That path is directionally right, but too thin for production. It splits into task strings plus dependency indexes, executes each node through broad stateless LLM turns, has no typed safety/approval plan, no aggregate tool provenance, and no useful partial-failure receipt.

Recommended design: compile each messy text message or voice transcript into a typed task graph, validate it against `config/tools.yaml`, run only safe independent nodes in bounded parallel lanes, keep same-resource writes serialized, let gatekeeper remain final enforcement for approvals, and return one compact Telegram receipt.

Internet research supports this. Multi-intent SLU work treats one utterance as multiple intents plus slots, not just a string split on "and"; recent work warns that simple conjunction-built datasets underrepresent real messy utterances ([Springer multi-intent SLU survey](https://link.springer.com/article/10.1007/s44336-025-00029-6), [IJCAI DPF](https://www.ijcai.org/proceedings/2024/715), [BlendX](https://aclanthology.org/2024.lrec-main.218.pdf), [AAAI HAOT](https://ojs.aaai.org/index.php/AAAI/article/view/29738)). Agent orchestration sources recommend parallelization only for cleanly decomposable subtasks, with guardrails at the tool boundary ([Anthropic](https://www.anthropic.com/engineering/building-effective-agents?cam=claude), [OpenAI Agents SDK orchestration](https://openai.github.io/openai-agents-python/multi_agent/), [OpenAI guardrails](https://openai.github.io/openai-agents-js/guides/guardrails/), [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api), [LangGraph branching](https://docs.langchain.com/oss/python/langgraph/use-graph-api)). Hermes Agent and OpenClaw reinforce the same lessons: isolate child work, narrow toolsets, cap concurrency, keep prompts self-contained, use durable background work for long jobs, and let the parent synthesize user-visible updates ([Hermes delegation](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation), [Hermes cron](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron/), [Hermes cron internals](https://hermes-agent.nousresearch.com/docs/developer-guide/cron-internals), [Hermes GitHub AGENTS.md](https://github.com/NousResearch/hermes-agent/blob/main/AGENTS.md), [OpenClaw overview](https://docs.openclaw.ai/), [OpenClaw tools](https://docs.openclaw.ai/tools), [OpenClaw sub-agents](https://docs.openclaw.ai/tools/subagents), [OpenClaw channels](https://docs.openclaw.ai/channels), [OpenClaw Lobster](https://docs.openclaw.ai/tools/lobster), [openclaw/lobster GitHub](https://github.com/openclaw/lobster), [OpenClaw exec approvals](https://docs.openclaw.ai/tools/exec-approvals)).

## 2. Current Hikari behavior from local code

### Telegram text path

`agents/telegram_bridge.py` routes text through daily-checkin handling, pending approval resolution, politeness refusal, affect scan, probabilistic reaction, belief-frame detection, and a typing heartbeat. Inside the heartbeat, it imports `extract_tasks`, `should_extract`, and `run_compound_turn`. If `should_extract(user_text)` is true and extraction returns more than one task, it calls `run_compound_turn(_tasks)` and then appends the raw user message. Otherwise it calls `respond()`, which persists the user row before `run_user_turn()`.

Implications:

- Compound turns bypass the normal live conversation session.
- Raw text compound turns are persisted only after execution succeeds.
- Belief-frame context is ignored on compound turns.
- If compound execution fails before append, the actual user request may be missing from persistence.

### Voice note path

`handle_voice()` downloads the Telegram `.ogg`, rejects overlong notes, transcribes via `tools.voice.transcribe_voice()`, applies politeness and affect scan to the transcript, appends a compact event row, and then runs the same compound extraction branch. If more than one task is extracted, it calls `run_compound_turn(_tasks)` instead of the normal `run_user_turn(prompt)`.

Implications:

- Voice compound turns lose the wrapper prompt that says this was a voice note and allows affect-aware reaction.
- The transcript is persisted before execution, which is safer than the text compound path.
- There is no transcript confidence / uncertainty handling around critical fields.

### Runtime/session model

`agents/runtime.py` has the right primitive split:

- `run_user_turn()` resumes the live SDK session and takes `_RUN_LOCK`.
- `run_visible_proactive()` resumes live state and takes `_RUN_LOCK`.
- `run_internal_control()` is stateless: `resume=None`, `log_session_id=False`, no memory injection, no `_RUN_LOCK`.

This makes internal-control calls safe for parallel child work with respect to live SDK session state. The open issue is tool scope: each subtask still gets the broad configured tool surface.

### Existing extractor and executor

`tools/dispatch/task_extractor.py`:

- `should_extract()` uses a regex over connective phrases in English, Ukrainian, and Russian plus an 8-word minimum.
- `extract_tasks()` calls the cheap aux model with a JSON-only prompt.
- Output is only `{"task": str, "depends_on": list[int]}`.
- Any extraction failure falls back to one task.

`agents/compound_turn.py`:

- Builds dependency waves with a topological sort.
- Runs independent nodes in a wave with `asyncio.gather()`.
- Uses `run_internal_control(task_text)` for every node.
- Hides partial exception details and raises only when all tasks fail.
- Joins successful replies with blank lines.

This gives Hikari a useful skeleton, not a full product contract.

### Tool policy and approval surface

`config/tools.yaml` is the policy source:

- Google Workspace sends/deletes/creates/uploads/edits are `gate: gatekeeper`.
- Google Workspace reads such as Gmail query/details and calendar events are ungated reads with untrusted output.
- `dispatch_claude_session` is gatekeeper-gated.
- In-process reminder and link-shelf writes are currently ungated local writes.
- `calendar_get_events` exists as an in-process typed read adapter.
- Apple Events user-facing writes are marked `gate: confirm_send`; `tools/gatekeeper_can_use_tool.py` treats `confirm_send` like a blocking approval path.

`tools/gatekeeper_can_use_tool.py` denies unknown tools, denies write/destructive wildcard matches without an explicit gate, rejects untrusted-origin content in tool args, and routes gated calls into `tools.gatekeeper.GATEKEEPER.request()`.

`tools/gatekeeper.py` is a durable async approval state machine: it writes an approval row, sends a Telegram approval prompt, waits for approval/rejection/timeout, is idempotent by `tool_use_id`, and expires/nudges stale approvals on restart.

### Relevant tool behavior

- `reminder_create` writes a local reminder row and queues Google Calendar / Apple sync asynchronously.
- `reminder_cancel` is idempotent by id.
- `reminder_snooze` mutates one reminder by id.
- `link_save` SSRF-safely fetches metadata, then writes to SQLite and wraps fetched metadata as untrusted for LLM-facing output.
- `calendar_get_events` is a typed read adapter over Google Workspace MCP.
- `dispatch_claude_session` creates a durable background task, runs a separate SDK session, streams progress events, and defaults to read-only tools unless the caller requests edit/write/bash.
- `agents.messaging.send_and_persist()` sends first, then persists only after confirmed Telegram delivery, recording final filtered text.

### Tests that matter

Current tests pin useful invariants:

- `tests/test_compound_turn.py`: extraction heuristic, fallback, parallel independent tasks, dependency ordering, partial failure.
- `tests/test_proactive_session_isolation.py`: `run_internal_control()` does not overwrite live `session_id`, including concurrent calls.
- `tests/test_gatekeeper.py` and `tests/test_gatekeeper_integration.py`: approval/rejection/timeout/idempotency/restart recovery and SDK hook behavior.
- `tests/test_google_workspace_send_policy.py`, `tests/test_destructive_tool_gating.py`, `tests/test_tool_policy_access_mode.py`: gated tools and wildcard fail-closed policy.
- `tests/test_post_filter_fabrication.py`: calendar/email claims require a relevant fetch tool in the turn.
- `tests/test_voice.py`: Hikari-voice constraints.

Main test gap: compound execution needs aggregate tool provenance. Otherwise a final calendar/Gmail receipt assembled from child turns may confuse the single-turn fabrication backstop.

## 3. Internet research findings with citations

### Multi-intent detection and compound commands

Multi-intent SLU is a joint multiple-intent detection and slot-filling problem: one utterance can contain several intents, each with its own arguments. Common benchmarks include MixATIS and MixSNIPS, and evaluation usually checks intent-set accuracy, slot F1, and combined sentence accuracy ([Springer multi-intent SLU survey](https://link.springer.com/article/10.1007/s44336-025-00029-6)).

Recent work emphasizes that data scarcity and entity/slot assignment are central problems. DPF-style work first handles task-agnostic entity spans, then task-specific labeling ([IJCAI DPF](https://www.ijcai.org/proceedings/2024/715)). Hikari should similarly extract spans and entities before binding them to tools.

BlendX is especially relevant because it criticizes shallow conjunction patterns in MixATIS/MixSNIPS-style data and adds blended, omitted, and coreferential command patterns ([BlendX](https://aclanthology.org/2024.lrec-main.218.pdf)). Hikari’s current regex gate is fine as a cheap guard, but not enough as the real definition of compound input.

HAOT-style work frames multi-intent understanding as intent-scope assignment with intent-slot interaction ([AAAI HAOT](https://ojs.aaai.org/index.php/AAAI/article/view/29738)). Product translation: every extracted node should carry an evidence span and its own argument scope.

### Agent orchestration, decomposition, and parallel execution

Anthropic distinguishes prompt chaining, routing, parallelization, orchestrator-workers, and evaluator-optimizer workflows. Parallelization is best when subtasks are independent; orchestrator-worker is better when subtasks are dynamically determined ([Anthropic](https://www.anthropic.com/engineering/building-effective-agents?cam=claude)).

OpenAI’s Agents SDK docs describe “agents as tools” versus handoffs. For Hikari, “agents/tools as bounded subtasks” is the right analogy because Hikari should keep control of the final user-facing receipt ([OpenAI Agents SDK orchestration](https://openai.github.io/openai-agents-python/multi_agent/)).

OpenAI’s guardrails docs note that guardrails are not automatically applied to every inner step in a multi-agent system, so tool guardrails are needed around the actual function-tool calls ([OpenAI guardrails](https://openai.github.io/openai-agents-js/guides/guardrails/)). This exactly matches Hikari’s gatekeeper: extraction-time classification is advisory; `can_use_tool` remains enforcement.

LangGraph models work as a state graph and runs multiple outgoing edges in parallel supersteps. Its docs also warn that combining static and dynamic routing can make behavior hard to reason about ([LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)). Hikari should compile one graph, not let every child turn invent new routing.

LangGraph branching docs add useful execution semantics: checkpointing, transactional supersteps, retrying failed branches without redoing successful work, and configurable concurrency ([LangGraph branching](https://docs.langchain.com/oss/python/langgraph/use-graph-api)).

### Voice assistant UX for multi-action requests

Google Assistant Routines let one voice command trigger multiple actions, but Google frames routines as convenience automation and excludes safety/security-critical actions ([Google Assistant Help](https://support.google.com/assistant/answer/7672035?hl=en-TM)). Hikari should auto-run low-risk compound work but ask approval for safety/security/external side effects.

Google’s Routine Suggestions docs say the triggering intent should provide its response and end the conversation without extra interaction ([Google Developers](https://developers.google.com/assistant/engagement/routines)). Hikari should avoid turning one voice note into a long back-and-forth; it should do safe work and ask one bundled clarification when needed.

Amazon’s Alexa routine guidance describes routines as shortcuts that group actions, recommends frequent/relevant actions, and warns against blocking the customer’s current task or duplicating information in handoffs ([Amazon Alexa Routines](https://developer.amazon.com/en-US/alexa/alexa-haus/routines)). Hikari’s progress messages should be aggregated, not per-tool chatter.

A CHI 2023 paper on voice assistant failures found that assistants often miss user expectations and that overcapturing input can be especially harmful to trust ([Baughan et al.](https://arxiv.org/abs/2303.00164)). Hikari should avoid silently acting on uncertain voice transcripts for recipients, times, sends, deletes, and payments.

## 4. Hermes/OpenClaw orchestration lessons

### Hermes Agent

Hermes uses platform-specific toolsets and explicit toolset keys, including Telegram-specific messaging support ([Hermes GitHub AGENTS.md](https://github.com/NousResearch/hermes-agent/blob/main/AGENTS.md)). Lesson: a compound subtask should receive only the tool family it needs, not the whole Hikari tool surface.

Hermes `delegate_task` can run one task or a parallel batch; children have isolated context, receive only goal/context fields, and are capped at 3 concurrent subagents by default. Leaf children cannot clarify, use memory, send messages, execute code, or delegate further ([Hermes delegation](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation)). Hikari’s child tasks should likewise be isolated and never message Telegram directly.

Hermes says `delegate_task` is synchronous and non-durable; long work should use cron or terminal/background mechanisms ([Hermes delegation](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation)). Hikari already has `dispatch_claude_session`, so long repo/code tasks should route there instead of hiding inside a chat turn.

Hermes cron runs in fresh sessions, supports skills, script-only jobs, dependency context via `context_from`, and disables cron tools inside cron executions to avoid recursion ([Hermes cron](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron/), [Hermes cron internals](https://hermes-agent.nousresearch.com/docs/developer-guide/cron-internals)). Hikari background tasks should be self-contained and recursion-safe.

### OpenClaw

OpenClaw is a self-hosted gateway across channels where the gateway owns sessions, routing, and delivery ([OpenClaw overview](https://docs.openclaw.ai/)). Lesson: Telegram should remain the delivery boundary; child tasks return data, and Hikari sends the reply.

OpenClaw’s tools docs distinguish typed tools, skills, plugins, and policy-filtered tool visibility; the model only sees tools that survive active profile, policy, provider, sandbox, channel, and plugin filters ([OpenClaw tools](https://docs.openclaw.ai/tools)). Hikari should apply this per task node.

OpenClaw subagents are background runs, isolated by default, optionally context-forked, and report completion back to the requester session; they do not receive the message tool ([OpenClaw sub-agents](https://docs.openclaw.ai/tools/subagents)). This is a strong model for Hikari receipts and progress.

OpenClaw channels docs note that text/media/reactions vary by channel and channel policy prevents bot loops ([OpenClaw channels](https://docs.openclaw.ai/channels)). Hikari should keep Telegram-specific progress and receipt formatting out of extraction.

OpenClaw Lobster provides deterministic workflow calls with JSON pipes, approval checkpoints, resumable tokens, timeouts, caps, and auditability ([OpenClaw Lobster](https://docs.openclaw.ai/tools/lobster), [openclaw/lobster GitHub](https://github.com/openclaw/lobster)). Hikari does not need a DSL first, but it does need durable compound-turn ids and resumable approval state.

OpenClaw exec approvals layer policy and local host approval before commands run ([OpenClaw exec approvals](https://docs.openclaw.ai/tools/exec-approvals)). Hikari should not bypass gatekeeper just because a task came from the compound planner.

## 5. Proposed compound-turn pipeline

1. Normalize input into `CompoundTurnInput`: source, raw text/transcript, display text, received time, timezone, voice metadata, attachments, and recent resolvable references.
2. Extract tasks into strict JSON with ids, spans, intent types, entities, time refs, dependencies, conditions, missing fields, risk class, approval policy, tool candidates, and confidence.
3. Validate deterministically: resolve dates, bind URLs/pronouns, reject ambiguous destructive targets, classify against `config/tools.yaml`, and mark untrusted read-to-write chains.
4. Plan graph states: `ready`, `needs_clarification`, `needs_approval`, `blocked`, `skipped`.
5. Execute safe ready nodes in waves with bounded parallelism and resource locks.
6. Use deterministic executors for common tools (`reminder_create`, `calendar_get_events`, `link_save`, `reminder_list`, `link_search`) when parameters are complete.
7. Use LLM child turns only for synthesis/drafting/unsupported work, with narrowed tool candidates.
8. Aggregate results, failures, approvals, clarification prompts, and tool provenance.
9. Send one compact receipt.

## 6. Intent extraction schema

Suggested task node:

```json
{
  "id": "t1",
  "intent_type": "reminder.create",
  "utterance_span": "remind me to call mom tomorrow",
  "normalized_request": "Create a reminder to call mom tomorrow.",
  "entities": {
    "person": "mom",
    "reminder_text": "call mom"
  },
  "time_refs": [
    {
      "raw": "tomorrow",
      "resolved_iso": "2026-05-26T09:00:00+02:00",
      "assumption": "default reminder time"
    }
  ],
  "tool_candidates": ["mcp__hikari_utility__reminder_create"],
  "target_resource": {"kind": "reminder", "id": null},
  "depends_on": [],
  "condition": null,
  "missing_fields": [],
  "risk_class": "local_write_low",
  "approval_policy": "none",
  "confidence": 0.92,
  "voice_uncertainty": false
}
```

Recommended intent types:

- `reminder.create`, `reminder.list`, `reminder.cancel`, `reminder.snooze`
- `link.save`, `link.search`, `link.update`, `link.delete`
- `calendar.read`, `calendar.create`, `calendar.update`, `calendar.delete`
- `gmail.search`, `gmail.read`, `gmail.draft`, `gmail.send`, `gmail.delete`
- `drive.search`, `drive.read`, `drive.write`, `drive.delete`
- `memory.recall`, `memory.write`, `task.close`
- `research.web`, `wiki.read`, `wiki.write`
- `code.dispatch`
- `smalltalk.emotional`, `answer.direct`, `clarify`

## 7. Parallel tool execution rules

Safe to run in parallel:

- Independent read-only tasks from different resources.
- Calendar read + Gmail search + reminder list.
- Weather, places, arxiv, currency, translate, ytmusic reads.
- Link metadata fetches before DB insert.
- Multiple read-only background dispatches, under a cap.
- Multiple stateless LLM synthesis tasks, as long as they do not use the live SDK session.

Run in parallel with limits:

- External reads against one MCP server: cap per server.
- Link saves: fetch in parallel, serialize inserts.
- Reminder creates: serialize if SQLite lock errors appear.
- Gmail details: batch/cap message-id fetches.
- Approval previews: prepare in parallel, prompt as one bundle when possible.

Must serialize:

- Mutations of the same Gmail thread/draft.
- Mutations of the same calendar event.
- Mutations of the same reminder id.
- Mutations of the same Drive/Docs/Sheets/Slides object.
- Memory writes that could contradict one another.
- Live SDK `run_user_turn()` and visible proactive calls, already protected by `_RUN_LOCK`.

Must block on approval:

- Gmail send/reply/bulk delete.
- Gmail draft create/delete/send, per current config.
- Calendar create/delete.
- Drive upload/create/delete.
- Docs/Sheets/Slides writes.
- Notion/GitHub writes.
- `dispatch_claude_session`.
- `python_run`.
- Apple Events `confirm_send` tools.

Must not auto-run:

- Destructive operations without a concrete id or exact target.
- Any write whose payload comes from untrusted external content unless summarized and approved.
- Low-confidence voice tasks touching recipients, times, sends, deletes, payments, or event moves.
- Unsupported “book/buy/order/pay/send” actions without the exact target, price, recipient, and payload.

## 8. Clarification and approval rules

Clarification answers “what exactly should I do?” Approval answers “I know what to do; may I do it?”

Clarify when:

- Required fields are missing: recipient, time, target id, event identity, email body, attachment/file, URL.
- A pronoun or “that/this” has multiple possible targets.
- Voice transcription is uncertain around a critical field.
- The user asks to move/cancel/delete and no unique target can be resolved.
- The requested capability/tool does not exist.
- Extracted tasks conflict.

Approve when:

- `config/tools.yaml` says `gatekeeper` or `confirm_send`.
- The task sends, deletes, creates, uploads, edits, or externally mutates.
- The task starts durable background code/repo work.
- A local write is driven by untrusted external content.

Immediate work can continue while clarification is pending when it is independent. Example: set the milk reminder now, clarify which gym event to move.

## 9. Telegram progress/receipt UX

Progress should be sparse:

- If everything finishes under about 4 seconds: no progress message.
- If work crosses about 4-6 seconds: one compact progress message, e.g. “working on 4 things: calendar, reminder, link, draft.”
- If background dispatch starts: use the existing ETA/final dispatch path.
- If approvals are needed: gatekeeper prompts remain the approval surface, ideally bundled when a compound planner can preview multiple related writes.
- Final reply is one receipt, not one reply per node.

Receipt shape:

```text
done: reminder, link, calendar check.
waiting: draft to Alex needs what you want it to say.
blocked: none.
```

Voice-note receipt should be shorter and should acknowledge uncertainty only when it matters:

```text
milk reminder is set, and i’m checking gym/bills separately. if gym is a calendar event, moving it needs approval.
```

## 10. Failure recovery behavior

Current `run_compound_turn()` suppresses failed partial nodes. Production behavior should be explicit:

- Mark each node `done`, `failed`, `blocked`, `skipped`, `needs_approval`, or `needs_clarification`.
- Keep successful results.
- Skip dependents when the parent fails.
- Include safe user-facing failure summaries.
- Offer one retry path only when retry is meaningful.
- Persist the task graph and node results for audit/debug.
- Aggregate tool provenance so post-filter fabrication checks know which child actually fetched calendar/Gmail data.

Examples:

- Calendar read fails, reminder succeeds: “reminder set. calendar check failed because Google auth is sick.”
- Gmail read succeeds, draft approval times out: “found the thread. draft was not created; approval timed out.”
- Voice transcript uncertain: “milk reminder set. type whether you meant bills or builds.”

## 11. 10 messy user examples with extracted task graphs

### Example 1

User: “remind me to call mom tomorrow, check if I have meetings before 12, save this link https://example.com, and draft Alex”

Tasks:

- `t1 reminder.create`: call mom tomorrow. No approval.
- `t2 calendar.read`: events tomorrow before 12. No approval.
- `t3 link.save`: save URL. No approval under current policy.
- `t4 gmail.draft`: draft Alex. Missing body/recipient disambiguation; gated once complete.

Parallel groups:

- Group A: `t1`, `t2`, `t3`.
- Group B: none; `t4` waits for clarification.

Approvals: none yet; draft approval only after recipient/body are known.

Final reply:

```text
done: reminder for mom, calendar check before noon, and link saved. alex still needs a draft body unless you want me to guess, which is how disasters get hobbies.
```

### Example 2

User: “voice note: I’m tired, move gym to tomorrow, tell me if I have bills due, and remind me to buy milk”

Tasks:

- `t0 smalltalk.emotional`: tired affect. No tool.
- `t1 reminder.create`: buy milk. No approval.
- `t2 calendar.read + reminder.list`: find “gym” target. No approval.
- `t3 gmail.search/calendar/reminder reads`: find likely bills due. No approval for reads.
- `t4 calendar.update or reminder.snooze`: move gym to tomorrow. Depends on `t2`; approval depends on resource type.

Parallel groups:

- Group A: `t1`, `t2`, `t3`.
- Group B: `t4` if exactly one gym item is found.

Approvals: calendar mutation needs gatekeeper; local reminder snooze needs exact reminder id.

Final reply:

```text
milk reminder is set. i’m checking gym and bills separately; if gym is a calendar event, moving it needs confirmation. also, tired noted. inconveniently.
```

### Example 3

User: “delete that old draft, send the invoice to Sam, and remind me Friday to check Stripe”

Tasks:

- `t1 reminder.create`: check Stripe Friday. No approval.
- `t2 gmail.delete_draft`: “that old draft” lacks draft id. Clarify.
- `t3 gmail.send`: send invoice to Sam. Missing exact recipient/content/attachment. Clarify, then approval.

Parallel groups: Group A: `t1`. Clarification bundle: `t2`, `t3`.

Approvals: delete draft and send email both require gatekeeper after fields are known.

Final reply:

```text
stripe reminder is set for friday. i need the draft id for “old draft” and which invoice/sam before i touch email.
```

### Example 4

User: “check the weather in Oslo and if it’s raining move my outdoor run to the gym, then tell Mira I’ll be late”

Tasks:

- `t1 weather_fetch`: Oslo weather. No approval.
- `t2 calendar/reminder.read`: find outdoor run. Can run in parallel as prep.
- `t3 calendar.update/reminder.snooze`: move run. Depends on rain condition and unique target.
- `t4 message.send`: tell Mira. Requires available messaging tool, recipient, and approval.

Parallel groups: Group A: `t1`, `t2`. Group B: `t3`, `t4` only if condition true and fields resolved.

Approvals: calendar mutation and outbound message.

Final reply:

```text
weather and run lookup are underway. if it’s actually rain and i find one run, moving it and messaging mira will need confirmation.
```

### Example 5

User: “summarize unread emails from Alex and if any mention the contract, draft a reply saying I’ll review it tonight”

Tasks:

- `t1 gmail.search`: unread from Alex. No approval.
- `t2 gmail.read`: details for matches. No approval.
- `t3 summarize`: synthesize. Depends on `t1/t2`.
- `t4 gmail.draft`: conditional draft reply. Depends on `t2`; approval required by current config.

Parallel groups: Group A: `t1`. Group B: parallel details fetch. Group C: `t3`, gated preview for `t4`.

Approvals: draft creation requires gatekeeper.

Final reply:

```text
alex has two unread. one mentions the contract; i’ve got the draft preview ready and it needs confirmation before i create it.
```

### Example 6

User: “save these: https://a.example https://b.example https://c.example and log that I shipped the compound-turn review”

Tasks:

- `t1 link.save`, `t2 link.save`, `t3 link.save`: one per URL.
- `t4 receipt_add`: shipped compound-turn review.

Parallel groups: Group A: metadata fetches for all URLs. Group B: serialized local DB writes.

Approvals: none under current policy.

Final reply:

```text
saved all three links and logged the ship. neat little stack of evidence, unfortunately.
```

### Example 7

User: “look up Hermes Agent and OpenClaw, compare the orchestration bits, and add the useful parts to the wiki”

Tasks:

- `t1 research.web`: Hermes official docs/GitHub.
- `t2 research.web`: OpenClaw official docs/GitHub.
- `t3 synthesize`: compare orchestration. Depends on `t1/t2`.
- `t4 wiki.write`: add useful parts. Depends on `t3`; untrusted-source-to-wiki write.

Parallel groups: Group A: `t1`, `t2`. Group B: `t3`. Group C: `t4`.

Approvals: recommended preview/approval for wiki write because source material is untrusted web text, even if current wiki write is not gatekeeper-gated.

Final reply:

```text
research is done. i found the useful bits; before i write them into the wiki, approve this summary so i don’t immortalize web sludge.
```

### Example 8

User: “book me dentist Thursday morning, check calendar first, and remind me the night before”

Tasks:

- `t1 calendar.read`: Thursday morning availability.
- `t2 appointment.book`: unsupported external booking; clarification/manual handoff.
- `t3 reminder.create`: night-before reminder. Depends on actual appointment time.

Parallel groups: Group A: `t1`. Group B: `t2/t3` blocked.

Approvals: booking would require approval if a tool existed.

Final reply:

```text
i can check the calendar, but i can’t book the dentist from here. once you give me the appointment time, i’ll set the night-before reminder.
```

### Example 9

User: “tell me what I have next week and cancel anything with standup if it’s after 6”

Tasks:

- `t1 calendar.read`: next week events.
- `t2 calendar.delete`: title contains standup and start after 18:00. Depends on `t1`.
- `t3 summarize`: next week schedule. Depends on `t1`.

Parallel groups: Group A: `t1`. Group B: `t3` plus approval preview for `t2`.

Approvals: calendar delete requires gatekeeper with exact event ids/titles/times.

Final reply:

```text
next week has 14 events. i found two after-6 standups; deletion needs confirmation with the event list.
```

### Example 10

User: “start read-only Codex workers to inspect repo A and repo B, and ping me when they’re done”

Tasks:

- `t1 code.dispatch`: repo A read-only.
- `t2 code.dispatch`: repo B read-only.
- `t3 progress.notify`: completion handled by dispatch listener.

Parallel groups: Group A: `t1`, `t2` if under dispatch concurrency cap.

Approvals: current `dispatch_claude_session` is gatekeeper-gated even when read-only.

Final reply:

```text
i can start both read-only workers. confirm the dispatch approvals and they’ll report back when done.
```

## 12. Suggested implementation phases

### Phase 1: Harden the existing prototype

- Persist raw text compound turns before execution.
- Add task ids to extractor output.
- Aggregate tool provenance across all child executions.
- Return explicit partial-failure receipts.
- Add bounded parallelism.
- Add tests for text and voice compound paths.

### Phase 2: Structured extraction schema

- Replace string-only extraction with the schema in section 6.
- Add deterministic validators for time, URLs, recipients, resource ids, and approval policy.
- Keep old extractor as fallback.
- Add eval fixtures for messy real utterances, not only “and also” examples.

### Phase 3: Deterministic executors for common tasks

- Directly execute `reminder_create`, `reminder_list`, `calendar_get_events`, `link_save`, `link_search`, and Gmail read flows when fields are complete.
- Use LLM subturns only for synthesis/drafting or unsupported workflows.
- Add per-resource locks and per-server concurrency caps.

### Phase 4: Approval bundling and receipts

- Add compound approval previews that group related external writes.
- Keep gatekeeper as final enforcement.
- Add a compound-turn status table with node ids, tool calls, approval ids, errors, and final status.

### Phase 5: Voice-specific hardening

- Add transcript uncertainty handling.
- Ask clarification when low-confidence spans affect recipients/times/actions.
- Keep Hikari’s affect-aware voice wrapper even when tasks execute through compound path.
- Add voice-note evals with disfluencies, corrections, and emotional content.

## 13. Suggested tests/evals

Unit tests:

- `should_extract()` catches comma-separated and semicolon-separated commands, not only “and also”.
- Extractor schema rejects missing task ids, invalid dependencies, bad timestamps, unknown intent types.
- Dependency graph detects cycles and produces stable wave ordering.
- Per-resource locking serializes same reminder/calendar/gmail mutations.
- Tool provenance aggregator records all tools from parallel child tasks.
- Partial failure returns completed/failed/blocked nodes, not blank omission.

Policy tests:

- Gmail/calendar/Drive writes never execute without gatekeeper approval.
- Untrusted external read-to-write flows require preview/approval.
- Unknown wildcard write/destructive tools still deny.
- Local link/reminder writes run without approval only when direct and complete.
- Multiple approval-needed tasks produce stable approval ids and either one bundled prompt or deterministic separate prompts.

Voice tests:

- Voice transcript with “move gym tomorrow and remind milk” runs reminder immediately and asks/approves only move.
- Low-confidence recipient/time blocks outbound send.
- Affective content is preserved in the final voice reply.
- Long voice note failure still returns the configured graceful reply.
- Voice compound turn persists transcript before execution.

Integration tests:

- Example 1: three tasks complete, draft clarifies.
- Example 2: milk reminder completes, gym move gates/clarifies, bills read runs.
- Calendar fabrication backstop passes when calendar read occurred inside a compound child.
- Calendar fabrication backstop fires when no calendar read occurred.
- Gatekeeper timeout in one node does not cancel unrelated completed nodes.
- Restart with pending compound approval marks node timed out and tells the user what to retry.

Eval set:

- 50 messy real-chat-style compound messages.
- 25 voice-note transcripts with disfluency and self-correction.
- 20 prompt-injection read-to-write cases.
- 20 ambiguous pronoun/resource cases.
- 20 partial-failure cases across Gmail, Calendar, links, reminders, and dispatch.
