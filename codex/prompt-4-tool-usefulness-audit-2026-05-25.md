# Prompt 4 - Tool Usefulness Audit

Date: 2026-05-25
Repo: `/Users/ol/agents/hikari-agent`
Review lens: daily user leverage, not tool count
Output: `codex/prompt-4-tool-usefulness-audit-2026-05-25.md`

## 1. Executive summary

Hikari has enough tools. The problem is not capability. The problem is deciding which tools deserve to be visible, which should quietly serve natural-language workflows, and which should be hidden until a real user scenario needs them.

The useful core is small:

- Reminders, proactive check-ins, memory/recall, wiki/search, link capture, day receipts, calendar/Gmail triage, attachment/voice/photo intake, weather/location context, and approval-gated workspace actions.
- Everything else should either be an internal support layer, an operator/debug command, or a specialized tool invoked only when the user clearly asks.

The best thing in the current design is not the tool inventory. It is the safety and routing spine: `config/tools.yaml`, ToolAnnotations, gatekeeper approvals, untrusted-output wrapping, audit tooling, and Telegram command tiers. That is real product infrastructure.

The weakest thing is Telegram usefulness. Several valuable tools exist but are not yet first-class enough in chat: link shelf, day receipt, decision log, wiki append/search, cross-source "find that thing", and safe approval dashboards. If the user cannot easily see, correct, approve, snooze, dismiss, or retrieve the result from Telegram, the tool is mostly theoretical.

Bluntly:

- Keep the daily workflow tools.
- Keep risky workspace/action tools, but only behind strong approval UX.
- Hide most novelty tools.
- Remove or defer stubs and tools with no recurring user job.
- Stop presenting "tool count" as a feature. Present "things Hikari does reliably every day."

## 2. Current Hikari tool surface from local code

Local files inspected:

- `config/tools.yaml`
- `tools/` including `_registry.py`, `_annotations.py`, `gatekeeper*`, reminders, link shelf, day receipt, dispatch, skills, photos, calc, wiki, Apple Notes, ytmusic, arxiv, places, weather, translate, currency
- `agents/telegram_bridge.py`
- `agents/cockpit.py`
- `agents/proactive.py`
- `agents/scheduler.py`
- `agents/compound_turn.py`
- `agents/engagement/`
- `README.md`
- `CLAUDE.md`
- Tests for tool inventory, annotations, destructive gating, Google Workspace gating, approvals, reminders, day receipt, link shelf, post-filtering, memory Telegram commands, proactive behavior

### Tool registry and enforcement

`config/tools.yaml` is the real policy surface. It defines:

- In-process MCP servers: memory, dispatch, utility, wiki, photo, codex reports, router.
- External MCP servers: Google Workspace, Notion, GitHub, Playwright, Apple Events, Apple Shortcuts, YouTube transcript, DuckDB.
- Native tools: `Agent`, `WebFetch`, `WebSearch`.
- Tool routing: `mcp__hikari_router__tool_search`.
- Gate behavior: `gatekeeper`, `confirm_send`, or no gate.
- Untrusted-output wrapping for web, memory recall, external reads, and external MCPs.
- Wildcard fail-closed behavior for unknown Google Workspace, Notion, and GitHub tools.

`tools/_annotations.py` mirrors MCP-style risk hints for in-process tools: read-only, write, destructive, local, external. The comments correctly say annotations are hints, not gates. Enforcement lives in `tools.yaml` and `gatekeeper_can_use_tool.py`.

`tools/_registry.py` auto-discovers utility tools from `tools/` packages with `ALL_TOOLS`, while skipping dedicated servers such as memory, photos, wiki, dispatch, codex, and router. This is good for maintainability but creates a product risk: adding a tool is easy, making it useful is not.

### Telegram surface

`agents/cockpit.py` already orders commands by usefulness:

- Daily: `/silence`, `/unsilence`, `/checkin`, `/memory`
- Weekly/needed: `/reminders`, `/status`, `/proactive`, `/tasks`, `/cancel`, `/help`
- Debug/operator: `/approvals`, `/settings`, `/capabilities`, `/tools`, `/audit`
- Hidden: `/start`, `/grab_stickers`, `/memory_diff`

That ordering is correct. The gap is that several actually useful tools are not yet command-surfaced: link shelf, day receipt, decision log, wiki, and cross-source retrieval.

`agents/telegram_bridge.py` has real input channels: text, reactions, photos, documents, voice, location, live location, and command handlers. It routes photo/document uploads through hard-scoped storage and `read_attachment`, handles owner lock, approval resolution, media outbox draining, typing heartbeat, compound-turn extraction, and post-send persistence. This is product-grade glue.

### Proactive and scheduled system

`agents/scheduler.py` runs a lot:

- Reminder firing and Apple/GCal sync
- Daily reflection, morning brief, daily check-in, evening diary
- Decision resolver, future letter, drift canary
- Engagement tick
- MCP warm-pool eviction, graph outbox drain, media outbox drain

`agents/engagement/producers/` has producers for Gmail, calendar, wiki changes, decisions, location, weather, Notion, Drive, re-engagement, weird mood leaks, and a Readwise stub. The engagement tick already parallelizes producer collection with `asyncio.gather`.

### Compound turns

`agents/compound_turn.py` runs independent extracted tasks in dependency waves. Within each wave, it uses `asyncio.gather`. This is the right architecture for "check weather and calendar and remind me later", but the high-value compound workflows should be explicitly tested.

### Tests

The repo has strong tests around:

- Tool inventory injection
- Tool annotation coverage and risk mapping
- Gatekeeper migration away from old defer gates
- Google Workspace/Notion/GitHub write gating
- Approval preview truthfulness
- Click-Allow/post-filter leakage
- Reminder creation/list/cancel/snooze/repeat/sync
- Day receipt lifecycle
- Link shelf behavior and SSRF-related safe fetch
- Telegram memory command behavior

The tests prove the tool platform is real. They do not yet prove that the right tools are visible in Telegram or that top workflows are excellent.

## 3. Internet research findings with citations

### Hermes Agent

Official Hermes docs position Hermes as a personal agent with a messaging gateway, tools/toolsets, persistent memory, skills, MCP integration, voice, security, cron, and broad platform delivery. The docs home lists these as first-class sections rather than one-off integrations: messaging gateway, 70+ built-in tools, memory, skills, MCP, security, and cron-style scheduled automations ([Hermes docs home](https://hermes-agent.nousresearch.com/docs/)).

Hermes GitHub makes the same product bet: a closed learning loop, scheduled automations, delegation/parallelism, multiple terminal backends, and migration from OpenClaw settings/memory/skills/allowlists ([Hermes GitHub](https://github.com/NousResearch/hermes-agent)).

Hermes tools research:

- Tools are grouped into toolsets; common toolsets include web/search, terminal, file, browser, memory, session search, cronjob, delegation, code execution, skills, messaging, media, and safe/read-only bundles ([Hermes Tools & Toolsets](https://hermes-agent.nousresearch.com/docs/zh-Hans/user-guide/features/tools), [Hermes Built-in Tools Reference](https://hermes-agent.nousresearch.com/docs/reference/tools-reference/), [Hermes Toolsets Reference](https://hermes-agent.nousresearch.com/docs/reference/toolsets-reference)).
- Hermes MCP support emphasizes external tool servers, automatic discovery/registration, per-server filtering, and runtime MCP toolsets named like `mcp-<server>` ([Hermes MCP](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp/), [Use MCP with Hermes](https://hermes-agent.nousresearch.com/docs/guides/use-mcp-with-hermes)).
- Hermes memory is bounded and curated, with `MEMORY.md`, `USER.md`, session search, and external memory providers such as Honcho and others ([Hermes Persistent Memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory/), [Hermes Memory Providers](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory-providers/)).
- Hermes cron supports one-shot/recurring tasks, skills attached to cron jobs, delivery to chat/files/platform targets, project workdirs, and no-agent script mode ([Hermes Scheduled Tasks/Cron](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron/)).
- Hermes messaging is a gateway across many channels, including Telegram, Discord, Slack, WhatsApp, Signal, SMS, Email, Matrix, Teams, LINE, and browser, with platform differences for voice, images, files, reactions, typing, and streaming ([Hermes Messaging Gateway](https://hermes-agent.nousresearch.com/docs/user-guide/messaging)).
- Hermes skills are procedural memory and reusable workflow instructions ([Hermes Skills System](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)).

Takeaway for Hikari: Hermes does not make every tool equally important. It uses toolsets, skills, memory, messaging, cron, and MCP as organizing layers. Hikari should do the same in Telegram: show workflows and tool families, not raw tool sprawl.

### OpenClaw

Official OpenClaw sources frame OpenClaw as a self-hosted, local-first personal assistant with channels, tools, skills, plugins, cron/background work, and safety controls. The GitHub README says OpenClaw answers on channels the user already uses and lists WhatsApp, Telegram, Slack, Discord, Google Chat, Signal, iMessage, Matrix, and others ([OpenClaw GitHub](https://github.com/openclaw/openclaw)).

OpenClaw tool research:

- OpenClaw docs distinguish tools, skills, and plugins: tools are typed actions sent to the model; skills are `SKILL.md` instruction packs; plugins add runtime capabilities such as tools, providers, channels, hooks, and packaged skills ([OpenClaw Tools Overview](https://docs.openclaw.ai/tools)).
- OpenClaw only shows tools that survive active profile, allow/deny policy, provider restrictions, sandbox state, channel permissions, and plugin availability ([OpenClaw Tools Overview](https://docs.openclaw.ai/tools)).
- OpenClaw explicitly calls out Tool Search for large tool catalogs, instead of sending every schema to the model ([OpenClaw Tools Overview](https://docs.openclaw.ai/tools)).
- OpenClaw channels are first-class. The channel docs list Telegram, Discord, Slack, WhatsApp, Signal, iMessage, Matrix, Teams, and more, and note that text/media/reactions vary by channel ([OpenClaw Channels](https://docs.openclaw.ai/channels)).
- OpenClaw skills can be workspace, shared, managed, or plugin-packaged; the GitHub docs describe `SKILL.md` format, load-time filters, environment injection scoped to an agent run, and skill override precedence ([OpenClaw skills docs on GitHub](https://github.com/openclaw/openclaw/blob/main/docs/tools/skills.md)).
- OpenClaw exec approvals are a strong model for local execution: command execution should only happen when policy, allowlist, and optional approval agree; the approval record binds command/cwd/session context so later caller edits cannot reuse stale approval ([OpenClaw Exec Approvals](https://docs.openclaw.ai/tools/exec-approvals), [OpenClaw exec-approvals source](https://github.com/openclaw/openclaw/blob/main/docs/tools/exec-approvals.md)).

Takeaway for Hikari: OpenClaw's useful lesson is not "have 5700 skills." It is capability scoping. Tools are filtered per channel/profile/context, and host execution approvals are treated as a system, not as a chat flourish.

### Personal productivity agent workflows

The common pattern across current productivity agents is not "tool variety." It is recurring, interruptible, user-visible workflows:

- OpenAI Tasks can run later, on a one-off or recurring schedule, while the user is offline, and notify by push/email ([OpenAI Help - Tasks in ChatGPT](https://help.openai.com/en/articles/10291617-scheduled-tasks-in-chatgpt?trk=public_post_reshare-text)).
- Zapier Agents are framed around triggers, actions, connected apps, and knowledge sources; users describe what triggers the agent, what tasks it performs, and which apps it uses ([Zapier Agents docs](https://help.zapier.com/hc/en-us/articles/24393442652557-Build-an-agent-in-Zapier-Agents)).
- Reclaim's value is automatic scheduling around priorities, deadlines, availability, and conflict rescheduling, not chatty "AI" for its own sake ([Reclaim scheduling docs](https://help.reclaim.ai/en/articles/6207587-how-reclaim-manages-your-schedule-automatically)).
- Todoist Assist focuses on making work actionable: task suggestions, completion tips, rewriting tasks, and breaking complex tasks into subtasks ([Todoist Assist docs](https://www.todoist.com/help/articles/introduction-to-todoist-assist-KgPP22q5O)).

Takeaway for Hikari: daily value comes from closed loops: remember, schedule, remind, summarize, prioritize, prepare, log, resurface, approve action, and follow through.

### Tool discoverability and command UX

Good command UX does not expose everything equally:

- Slack uses slash/shortcut menus to quickly take common actions, browse recently used shortcuts, and show required formatting while typing ([Slack shortcuts and slash commands](https://slack.com/help/articles/360057554553-Use-shortcuts-to-take-actions-in-Slack)).
- Discord application commands use names, descriptions, options, groups/subcommands, validation, and autocomplete to help users find and invoke commands ([Discord Application Commands](https://docs.discord.com/developers/interactions/application-commands)).
- The VS Code Command Palette is useful because it is searchable and action-oriented, not because every command is equally prominent ([GitHub docs on VS Code Command Palette in Codespaces](https://docs.github.com/en/codespaces/reference/using-the-vs-code-command-palette-in-codespaces)).

Takeaway for Hikari: Telegram should surface a small action menu, not a full tool registry. `/tools` belongs to operator/debug. User workflows need commands like `/receipt`, `/links`, `/decision`, and better approval cards.

### Safe tool execution and approval UX

The safety literature and product docs converge on the same thing: risky actions need deterministic host controls, previewable approvals, and resumable state.

- OpenAI Agents SDK human-in-the-loop flow pauses before approval-required tools, returns interruptions, lets the app approve/reject, and resumes from run state ([OpenAI Agents SDK human-in-the-loop](https://openai.github.io/openai-agents-js/guides/human-in-the-loop/)).
- Claude Code defaults to read-only permissions and asks explicit permission for editing files, running tests, and executing commands; users are responsible for reviewing actions before approval ([Claude Code security docs](https://code.claude.com/docs/en/security)).
- MCP tool annotations provide a risk vocabulary such as `readOnlyHint`, `destructiveHint`, `idempotentHint`, and `openWorldHint`, but the MCP blog is explicit that annotations are not enforcement; hard guarantees need sandboxing/network controls/policy ([MCP Tool Annotations blog](https://blog.modelcontextprotocol.io/posts/2026-03-16-tool-annotations/)).
- OpenClaw exec approvals add concrete host execution guardrails: effective policy, allowlists, ask modes, fallback deny, command binding, and chat/UI approval paths ([OpenClaw Exec Approvals](https://docs.openclaw.ai/tools/exec-approvals)).

Takeaway for Hikari: Hikari is already close here. The gatekeeper system is a strength. The remaining work is to close exceptions and make approval prompts easier to inspect from Telegram.

## 4. Hermes/OpenClaw tool UX lessons

1. Toolsets beat raw tools.
   Hikari should talk about "remember", "schedule", "search my stuff", "act with approval", "run a background code task", and "brief me", not 80 individual tool IDs.

2. Skills are useful only when curated.
   Hermes and OpenClaw treat skills as reusable workflow memory. Hikari's `skill_create`, `skill_approve`, and `run_skill` should stay hidden until there is a clear review/approval UX.

3. Messaging channel UX is the product.
   Hermes and OpenClaw both put serious weight on gateways and channels. Hikari lives in Telegram, so Telegram must expose the controls: approve/reject, snooze/dismiss, why this proactive, manage sources, find links, print receipt, correct memory.

4. Cron/proactive is only valuable with management controls.
   A scheduled or proactive system without "why", "snooze", "disable this source", and reaction feedback becomes noise. Hikari has much of the backend; surface it harder.

5. Local execution is a job queue, not a chat trick.
   Dispatch/code execution should have progress, cancel, preview, final summary, and approvals. Do not make it feel like a casual tool call.

6. Large catalogs need tool search and profiles.
   OpenClaw explicitly points to Tool Search for large catalogs. Hikari already has `mcp__hikari_router__tool_search`; use it to reduce schema noise and wrong-tool selection.

## 5. Tool family classification table

| Tool family | Classification | Daily value | Surface | Approval / parallel notes |
|---|---|---:|---|---|
| Reminders: `reminder_create/list/snooze/cancel`, Apple/GCal sync | Core daily value | High | Surface strongly in Telegram with list, snooze, dismiss | Create from user intent should be frictionless; cancel/snooze via buttons; sync writes should stay internal |
| Memory: `recall`, `remember`, `session_search`, tasks | Core daily value | High | Surface `/memory` and natural recall | `update_core_block` and invalidation are powerful; core-block writes should be approval-gated or command-only |
| Wiki: `wiki_search/read/list/tree/backlinks/append`, morning brief | Core daily value / occasional high leverage | High if used | Add Telegram wiki search/append shortcuts | Append can be natural for small notes; large/structural edits need preview |
| Link shelf: `link_save/search/list/update/delete` | Core daily value, needs UX | High | Add `/links` list/search/kind controls | Save should be automatic on URL share; delete needs confirmation |
| Day receipt: `receipt_add/today/get/print/week/search/set_note/delete` | Core daily value, needs UX | High | Add `/receipt` and quick category buttons | Delete needs confirmation; weekly print is high leverage |
| Daily check-in / morning brief / calendar preview | Core daily value | High | Keep `/checkin`; make result compact and actionable | Fetch calendar, Gmail, weather, reminders, open loops in parallel |
| Gmail/Google Workspace reads | Core daily value / occasional high leverage | High | Surface as check-in, triage, "find email/file" | Read-only but untrusted output; parallelize search/details/attachments |
| Google Workspace writes | Risky but useful | High when needed | Approval cards, not raw tool names | Never without approval: send/reply/delete/create/edit/upload |
| Calendar reads/writes | Core daily read; risky useful write | High | Surface calendar brief and event prep | Reads parallelize; create/delete/update events need approval unless scheduler-internal mirror |
| Attachments: `read_attachment`, photo/document/voice handlers | Core daily value | High | Natural upload UX, not a command | Keep hard path scoping; treat extracted text as untrusted |
| Weather/location: `weather_fetch`, location store, alerts | Core daily value | Medium-high | Natural and morning/check-in | Parallel with daily brief; avoid overusing location |
| Decision log: `decision_log_capture/resolve` | Occasional high leverage, needs UX | Medium | Add `/decision` pending/resolved controls | Capture natural predictions; weekly resolver already good |
| Apple Notes: `note_create/search/read` | Occasional high leverage | Medium | Natural "put this in Notes" | Note creation should confirm target; durable knowledge belongs in wiki |
| Translate/currency/calc | Occasional utility | Medium | Hide behind natural language | Safe to run without approval; parallel when part of a larger ask |
| `python_run` | Risky but useful | Medium | Hide | Keep approval; sandbox is good but not a daily chat affordance |
| Dispatch/code worker: `dispatch_claude_session` | Risky but high leverage | Medium-high for dev work | Surface as background task with `/tasks` and `/cancel` | Never without approval for Edit/Write/Bash; progress/cancel/final required |
| GitHub MCP/subagent | Occasional high leverage | Medium | Natural repo/PR/issue requests | Reads okay; writes/merge/delete/create PR need approval |
| Notion MCP/subagent | Occasional high leverage | Medium if user uses Notion | Natural Notion requests | Writes/comments/moves need approval |
| Apple Events | Risky but useful | Medium | Natural reminders/calendar only | Existing `confirm_send` is right; do not invent click-Allow UI |
| Apple Shortcuts | Risky but undercontrolled | Unknown | Hide | Current wildcard is destructive with no gate; should be fail-closed or approval-gated before LLM use |
| YouTube transcript | Occasional high leverage | Medium | Trigger only on YouTube link + summarize/discuss intent | Read-only, untrusted output |
| arXiv search | Occasional high leverage | Low daily | Hide unless user asks for ML/DL papers | Use in research brief, not default conversation |
| Places/OSM | Occasional | Low-medium | Natural "is X open" only | Coverage caveat required; location context helps |
| YT Music tools | Toy / niche | Low | Hide | Only when user asks about music history/library |
| Photo generation / stickers | Toy / emotional flavor | Low | Explicit ask only; keep caps | Do not present as productivity feature |
| Playwright MCP | Infrastructure / specialist | Low direct | Hide | Browser automation is not Telegram UX unless a workflow needs it |
| DuckDB MCP | Infrastructure / specialist | Low direct | Hide | Useful for analytics over uploaded/local docs; cap/timeout are good |
| Codex reports | Infrastructure / occasional dev | Low direct | Operator/dev only | Read-only |
| Router/tool_search | Infrastructure only | None direct | Hide | Essential for reducing tool overload |
| Tool inventory, `/tools`, `/audit`, `/capabilities` | Infrastructure/operator | Low direct | Keep operator tier | Do not confuse with user-facing value |
| Skills management | Infrastructure/risky | Low until UX exists | Hide | `run_skill` and approval should be gated/reviewed |
| Engagement producers | Core infra for proactive | High if tuned | Surface through `/proactive` | Keep parallel collection; remove/defer stubs |
| Readwise producer stub | Toy / remove-defer | None | Hide/remove | Stub explicitly says Readwise MCP removed |

## 6. Top 10 workflows Hikari should make excellent

1. Remind me and let me recover from it.
   Natural time parsing, real Telegram push, snooze/dismiss buttons, repeat handling, and list active reminders.

2. Morning check-in.
   Calendar, unread/important Gmail, weather, reminders, open tasks, and one useful nudge. This should be short, not a report blob.

3. Find that thing.
   Search memory, session history, wiki, link shelf, Drive, Notion, and Gmail depending on the user phrase. The output should identify source and confidence.

4. Save this link and bring it back later.
   Auto-save URLs, tag lightly, and resurface one relevant saved link when a topic returns. Add `/links`.

5. Day receipt.
   "Logged." for made/moved/learned/avoided. Print today and week on demand. Add `/receipt` with category buttons.

6. Decision calibration.
   Capture predictions with probability/date. Resolve later. Show pending decisions. Add `/decision`.

7. Meeting/event prep.
   Use calendar event, related Gmail, Drive docs, wiki notes, and prior messages to brief the user before the event.

8. File/photo/voice intake.
   User uploads something and Hikari understands it, extracts what matters, and takes the requested action. No raw path/tool chatter.

9. Approval-gated workspace actions.
   Draft/send email, create calendar event, edit doc/sheet/slide, create GitHub issue/PR, or update Notion with a full preview and explicit approval.

10. Background code/repo task.
    Dispatch a repo task, show progress, allow cancel, and return a concise final report. Do not use it for simple chat answers.

## 7. What to surface in Telegram

Surface these as real Telegram affordances:

- `/reminders`: already present. Add inline snooze/dismiss buttons if not complete everywhere.
- `/checkin`: keep prominent.
- `/memory`: keep; add friendlier correct/forget/search flows.
- `/links`: list/search/recent/tag/delete.
- `/receipt`: add made/moved/learned/avoided buttons, today/week/print.
- `/decision`: pending predictions, resolve, calibration history.
- `/wiki`: search/read/append recent note, or fold into `/memory` as "knowledge".
- `/proactive`: already present; make "why this?", snooze source, disable source, and recent sends obvious.
- `/approvals`: keep, but approval prompts should have inline approve/reject where Telegram supports it plus the typed phrase fallback.
- `/tasks` and `/cancel`: keep for dispatch/background work.

Do not surface raw tool IDs to normal use. `/tools`, `/audit`, and `/capabilities` should stay operator/debug.

## 8. What to hide

Hide these from everyday menus:

- Raw MCP server names and raw MCP tool IDs.
- `tool_search`, registry, annotations, inventory, and warm-pool details.
- `python_run`, Playwright, DuckDB.
- Skills management.
- `update_core_block`.
- `run_skill`.
- `ytmusic_*`, unless the user asks about music.
- `arxiv_search`, unless the user asks for recent ML/DL papers.
- Places search, unless the user asks about a place/opening hours.
- Photo generation and stickers, unless the user explicitly asks or an already-designed mood gate fires.
- Codex reports, except in dev/operator contexts.

Hidden does not mean deleted. It means the user should not have to think about these tools.

## 9. What to remove/defer

Remove or defer:

- `readwise_daily_review` producer until a working Readwise integration exists. A stub in proactive logic is product debt.
- YT Music as a visible feature. Keep the tools only as natural-language niche support.
- arXiv as a general chat capability. Keep it for explicit research requests.
- Photo generation as a productivity feature. Keep it as an explicitly requested emotional/media feature with caps.
- Skill creation/approval/run as user-facing features until there is a review surface. Right now it is too easy to confuse "I can make skills" with "this is safe and useful."
- Apple Shortcuts LLM access until approval gating is tightened.
- Any "capability" that does not map to a recurring workflow, a clear occasional high-leverage action, or infrastructure needed for the above.

## 10. Approval and safety recommendations

Hikari should never run these without approval:

- Gmail send/reply/bulk delete/send draft/delete draft.
- Google Calendar create/delete/update through Google Workspace.
- Drive upload/delete/create folder, Docs/Sheets/Slides creation or edits.
- Notion writes, block/page/database changes, moves, comments.
- GitHub writes: create/update issues, create PR, merge PR, create/update/delete files, branch/repo changes, reviews/comments.
- Dispatch/code worker when `allowed_tools` includes Edit, Write, Bash, or equivalent host-changing powers.
- `python_run`, even with sandboxing, because user intent and data exposure matter.
- Apple Events writes when initiated by the LLM rather than scheduler-internal sync.
- Apple Shortcuts.
- `run_skill`, `skill_approve`, and any future skill install/import.
- `update_core_block`.
- Destructive local deletes: `link_delete`, `receipt_delete`, `reminder_cancel`, `mark_fact_invalid` should at least require explicit Telegram command/button context, even if not full gatekeeper.

Keep and strengthen current good patterns:

- Gatekeeper typed approval lifecycle.
- Critical-field previews in full.
- Untrusted-origin argument denial.
- Wildcard fail-closed for unknown external write/destructive tools.
- PostToolUse untrusted output wrapping.
- Audit logs.
- ToolAnnotations for UX/risk hints, while keeping enforcement in deterministic policy.

Fix the main gap:

- `mcp__apple_shortcuts__*` is classified as destructive but currently has no gate. That should not be LLM-callable without an explicit gate or a hard allowlist of safe shortcuts.

Approval prompt UX should always show:

- Tool family in human words.
- Exact target account/repo/doc/event/file.
- Exact action.
- Full critical fields or diff/preview.
- Whether inputs came from user text or untrusted tool output.
- Timeout.
- Approve once, reject, and cancel path.

## 11. Parallelization recommendations

Already good:

- Engagement producers are collected in parallel.
- Compound turns run independent dependency waves in parallel.

Parallelize these compound turns deliberately:

- Daily check-in: Gmail unread/important, calendar events, reminders, weather, open memory tasks, and receipt summary.
- "What is my day?": calendar + weather + reminders + task recall + receipt today.
- "Find that thing about X": memory recall + session search + wiki search + link search + Drive/Notion/Gmail search when relevant.
- Meeting prep: calendar event + related Gmail + Drive docs + wiki + link shelf.
- URL share with a research request: link save + page fetch/research + wiki/link search for prior context.
- Uploaded file/photo: file validation/read + intent classification + relevant memory/wiki search after the file is safely stored.
- Travel/place question: weather + places/open-now + calendar/location context.
- Research brief: web search/fetch + arXiv if ML/DL + wiki/link search for prior saved sources.

Do not parallelize these blindly:

- Multiple writes to the same external object.
- Multiple approvals in one turn without a grouped preview.
- Scheduler sync writes and user-initiated writes to the same reminder/event.
- Memory/core-block mutations with other writes that depend on their result.

The product rule: parallelize reads and independent reasoning; serialize writes and approvals.

## 12. Suggested tests/evals

Add usefulness evals, not just registry tests:

- Daily workflow eval: "what is my day?" must call calendar, reminders, weather, and relevant memory in parallel, then return a compact answer.
- Retrieval eval: "what was that link/doc/email/note about X?" must search the right sources and cite source locations.
- Reminder UX eval: natural-language reminders create real rows; fired reminders include snooze/dismiss buttons; no LLM paraphrase at fire time.
- Link shelf eval: sharing a URL saves it; later related topic resurfaces one relevant link, not a list dump.
- Receipt eval: logging made/moved/learned/avoided is one-turn and low-friction; weekly print is readable.
- Decision log eval: probability + resolve date captures; resolver asks when due; outcome updates calibration.
- Approval eval: every risky Google/Notion/GitHub/dispatch/Apple/skill/shortcut action pauses with a truthful preview.
- Apple Shortcuts regression: destructive wildcard without a gate must be denied.
- Skill safety regression: `run_skill` cannot execute without a review/approval path.
- Tool restraint eval: Hikari must not use arXiv/YT Music/photo generation/places unless the user intent actually calls for it.
- Telegram command eval: `/help` lists daily commands first and hides debug-only items from normal command menus.
- Proactive quality eval: every proactive send has a source, reason, cooldown, and source-level snooze/disable path.
- Parallelization eval: compound read-only tasks use one wave; dependent writes wait.
- Latency/cost eval: daily check-in stays within a target wall-clock and model-call budget.

## Bottom line

Hikari should not add more tools right now. Hikari should make the top ten workflows excellent, hide the rest, and tighten the few approval gaps. The strongest version of this agent is not "an assistant with many tools." It is "a Telegram-native personal system that remembers, reminds, finds, briefs, logs, and acts only when the user can inspect the action."
