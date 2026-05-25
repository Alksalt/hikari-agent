# Hikari Tool Usefulness Audit

Date: 2026-05-25  
Scope: local Hikari repo plus official/primary external research.  
Lens: real user leverage, not tool count.

## 1. Executive Summary

Hikari has a lot of tools. That is not the same as having a lot of daily value.

The useful center is much smaller than the registry: reminders, calendar, important email, memory/recall, wiki/links, attachment ingestion, daily receipt, decision log, weather/location, and safe external actions. Most of the rest is infrastructure, occasional leverage, or should stay hidden unless explicitly requested.

Blunt read:

- Hikari's tool registry is broad enough. The next bottleneck is not "more tools"; it is **Telegram product surface**, **workflow composition**, and **approval clarity**.
- `config/tools.yaml` currently registers 165 tool ids. That is impressive only to the maintainer. To the user, it is noise unless grouped into a few reliable workflows.
- The strongest daily-value tools are already present: reminders, calendar, Gmail/Drive read, memory, wiki, link shelf, attachments, weather, day receipt, and proactive engagement. They need better Telegram affordances and more compound-turn parallelism.
- The riskiest useful tools are external writes: Gmail send/delete, Calendar writes, Drive/Docs/Sheets/Slides writes, GitHub/Notion writes, Apple Shortcuts, `dispatch_claude_session`, `python_run`, and memory/core-block mutation. Keep them, but gate them hard.
- The toy layer should not be removed from the codebase yet, but it should be hidden from the primary tool story: photo generation, music lookups, stickers, mood leaks, and ceremonial proactives are personality features, not productivity features.
- Hermes Agent and OpenClaw both point to the same lesson: toolsets/skills/tool-search/approvals exist to **reduce visible surface area** and make agents safer, not to advertise capability counts. Hermes documents broad toolsets, MCP, skills, cron, memory, messaging, and platform presets; OpenClaw emphasizes channels, automations, tool search, skills, local execution, and exec approvals. Hikari should steal the product lesson, not the tool-count race. Sources: [Hermes docs](https://hermes-agent.nousresearch.com/docs/), [Hermes tools](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools/), [Hermes toolsets](https://hermes-agent.nousresearch.com/docs/reference/toolsets-reference/), [OpenClaw docs index](https://docs.openclaw.ai/llms.txt), [OpenClaw automation](https://docs.openclaw.ai/automation), [OpenClaw tool search](https://docs.openclaw.ai/tools/tool-search), [OpenClaw exec approvals](https://docs.openclaw.ai/tools/exec-approvals).

Top recommendation: make Hikari excellent at 10 workflows, not 165 tools.

## 2. Current Hikari Tool Surface From Local Code

Local files inspected:

- `config/tools.yaml`
- `tools/`
- `tools/_annotations.py`
- `tools/_registry.py`
- `tools/_utility_index.py`
- `tools/gatekeeper.py`
- `tools/approvals.py`
- `agents/telegram_bridge.py`
- `agents/cockpit.py`
- `agents/proactive.py`
- `agents/engagement/`
- `agents/tool_inventory.py`
- `agents/scheduler.py`
- `README.md`
- `CLAUDE.md`
- tests covering user-facing tools, gates, untrusted wrapping, reminders, receipts, links, attachments, Telegram UX, and annotations

Observed registry shape:

- 165 registered tool ids in `config/tools.yaml`.
- 59 tools marked `gate: gatekeeper`.
- 85 tools marked `untrusted_output: true`.
- 27 tools marked `access_mode: write`.
- 1 tool marked `access_mode: destructive`.
- 6 tools marked `gate: confirm_send`.

Major Hikari families:

- Native foundation: `Agent`, `WebSearch`, `WebFetch`.
- Router/discovery: `hikari_router`, `tool_search`, `agents/tool_inventory.py`, `/tools`, `/audit`, `/status`.
- Memory: `recall`, `remember`, `mark_fact_invalid`, `update_core_block`, `task_create`, `task_update`, `session_search`.
- Reminders/calendar: `reminder_create`, `reminder_list`, `reminder_cancel`, `reminder_snooze`, `calendar_get_events`, Apple/GCal sync jobs.
- Google Workspace: Gmail, Calendar, Drive, Docs, Sheets, Slides, mostly read ungated and write gated.
- Wiki: `wiki_search`, `wiki_read`, `wiki_append`, `wiki_backlinks`, `wiki_list`, `wiki_tree`.
- Attachments/media: `read_attachment`, photo/document/voice/location handling in `agents/telegram_bridge.py`, `generate_photo`.
- Personal utilities: `weather_fetch`, `currency_convert`, `translate`, `places_search`, `place_open_now`, `arxiv_search`, `ytmusic_recent`, `ytmusic_search`, `ytmusic_library`.
- Capture/reflection: link shelf, day receipt, decision log.
- Local execution/data: `calc`, `python_run`, DuckDB MCP, Playwright MCP.
- External systems: Notion, GitHub, Apple Events, Apple Shortcuts, YouTube transcript.
- Delegation/reports: `dispatch_claude_session`, `list_codex_reports`, `read_codex_report`.
- Safety/infrastructure: gatekeeper, approvals, annotations, external wrap, sanitizer tests, SSRF tests, tool inventory, audit logs, scheduler jobs.
- Proactive engagement: reminders, morning brief, calendar prep, Gmail candidates, weather alert, wiki/drive/notion changes, decision resolution, callback episodes, silence re-engagement, mood leaks.

Important Telegram surface:

- `agents/telegram_bridge.py` already supports `/start`, `/silence`, `/unsilence`, `/tasks`, `/cancel`, `/memory_diff`, `/memory`, `/approvals`, `/proactive`, `/help`, `/status`, `/tools`, `/audit`, `/settings`, `/grab_stickers`, `/reminders`, `/checkin`.
- Inline callbacks already exist for approvals, reminder snooze/dismiss, and check-in actions.
- Voice transcription, document ingestion, photo classification, live location, and media outbox are real product surface, not theoretical tool plumbing.
- `agents/cockpit.py` is the command registry source of truth and has good operator affordances, but much of it is maintainer-facing rather than user-facing.

Local tests are stronger than average for a personal agent:

- `tests/test_destructive_tool_gating.py` checks external write gating.
- `tests/test_gatekeeper_integration.py` checks approve/reject flows and bulk-delete gating.
- `tests/test_external_wrap.py` checks untrusted output wrapping.
- `tests/test_read_attachment_path_validation.py` checks hard-scoped attachment reads.
- `tests/test_link_shelf_ssrf.py` checks SSRF defenses.
- `tests/test_reminders_tool.py`, `tests/test_day_receipt.py`, and `tests/test_link_shelf.py` cover high-value personal tools.

The gap is not absence of implementation. The gap is turning tool families into obvious, low-friction Telegram workflows.

## 3. Internet Research Findings With Citations

### Hermes Agent

Hermes presents tools as grouped capabilities, not a flat list. Its docs say tools are functions extending the agent and are organized into logical toolsets enabled per platform. Its built-in categories include web, terminal/files, browser, media, orchestration, memory, automation/delivery, and integrations. Source: [Hermes Tools & Toolsets](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools/).

Hermes toolsets are named bundles that control what an agent can do per platform, session, or task. This is the right abstraction for Hikari: the user should never see a 165-item list; Hikari should have task-level bundles such as "daily check-in", "email approval", "research packet", "memory/wiki recall", and "external write". Source: [Hermes Toolsets Reference](https://hermes-agent.nousresearch.com/docs/reference/toolsets-reference/).

Hermes supports dynamic MCP toolsets: each configured MCP server becomes a `mcp-<server>` toolset, with include/exclude filtering. This maps directly to Hikari's Google/Notion/GitHub/Playwright/DuckDB external MCP surface: expose only the tools needed for a workflow, not everything the server can do. Sources: [Hermes Toolsets Reference](https://hermes-agent.nousresearch.com/docs/reference/toolsets-reference/), [Hermes MCP Config Reference](https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference/).

Hermes skills are a procedural-memory layer: installed skills can be listed/searched and loaded progressively only when needed. Hikari already has project skills and local instructions; the lesson is to keep reusable workflows in skill-like packets and load them on demand. Source: [Hermes Working with Skills](https://hermes-agent.nousresearch.com/docs/guides/work-with-skills/).

Hermes has scheduled automation, messaging delivery, memory, and GitHub-hosted source. Sources: [Hermes Cron](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron/), [Hermes Messaging Gateway](https://hermes-agent.nousresearch.com/docs/user-guide/messaging), [Hermes Persistent Memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory/), [NousResearch/hermes-agent GitHub](https://github.com/NousResearch/hermes-agent).

### OpenClaw

OpenClaw positions itself as a self-hosted gateway connecting many messaging channels to coding agents. Its docs index lists Discord, Google Chat, iMessage, Matrix, Microsoft Teams, Signal, Slack, Telegram, WhatsApp, Zalo, and more. Hikari's Telegram-first strategy is narrower, but that is a strength if Telegram becomes excellent. Source: [OpenClaw docs index](https://docs.openclaw.ai/llms.txt).

OpenClaw channel docs say channels can run simultaneously, route per chat, and enforce DM pairing/allowlists. Hikari already has single-owner Telegram and access controls; the lesson is to treat channel context as product design, not just transport. Source: [OpenClaw Channels](https://docs.openclaw.ai/channels).

OpenClaw automation separates exact cron jobs, heartbeat checks, inferred commitments, task ledgers, task flow, hooks, and standing orders. That is a useful taxonomy for Hikari. Hikari currently has reminders, heartbeats, proactive engagement, scheduler jobs, and task/memory loops, but Telegram should explain these as separate user concepts: exact reminders, periodic check-ins, inferred follow-ups, and background jobs. Source: [OpenClaw Automation](https://docs.openclaw.ai/automation).

OpenClaw Tool Search is explicitly designed for large tool catalogs: the model sees compact descriptors, searches them, describes one selected tool when needed, and calls through normal policy/approval/logging. That is directly relevant to Hikari's 165 tools. The right default is contextual discovery, not exposing every schema. Source: [OpenClaw Tool Search](https://docs.openclaw.ai/tools/tool-search).

OpenClaw skills are local/workspace skill documents, and its docs warn to treat third-party skills as untrusted code. Hikari's skills are currently local and curated; do not turn skills into an unreviewed marketplace import path. Source: [OpenClaw Skills](https://docs.openclaw.ai/tools/skills).

OpenClaw exec approvals require policy, allowlist, and optional user approval to agree before host commands run. It also records canonical command/cwd/context and rejects drifted approvals. Hikari's gatekeeper is directionally right; local execution and code dispatch should be held to the same standard. Source: [OpenClaw Exec Approvals](https://docs.openclaw.ai/tools/exec-approvals). Official source repo: [openclaw/openclaw GitHub](https://github.com/openclaw/openclaw).

### Personal Productivity Agent Workflows

OpenAI Scheduled Tasks emphasize future and recurring work delivered back to the user. The relevant Hikari lesson is simple: reminders must be reliable, inspectable, editable, pausable, and obvious. Source: [OpenAI Help: Tasks in ChatGPT](https://help.openai.com/en/articles/10291617-scheduled-tasks-in-chatgpt).

OpenAI ChatGPT agent combines browsing, code, apps/connectors, terminal, files, confirmation, and scheduling. The useful part is not "it has tools"; it is that it can move between research and action while pausing for confirmation and letting the user interrupt. Sources: [OpenAI Help: ChatGPT agent](https://help.openai.com/en/articles/11752874-chatgpt-agent), [OpenAI: Introducing ChatGPT agent](https://openai.com/index/introducing-chatgpt-agent/).

Microsoft 365 Copilot's official overview frames productivity value around work context: Word documents, Excel formulas, Outlook email-thread summaries, and Teams meeting summaries. Hikari's strongest equivalent is "what matters today across calendar, email, notes, tasks, and memory?" Source: [Microsoft 365 Copilot overview](https://learn.microsoft.com/en-us/copilot/microsoft-365/microsoft-365-copilot-overview).

Reclaim's feature docs are a useful benchmark because they focus on defended time, tasks, habits, smart meetings, calendar sync, buffers, and planning. This is the right kind of daily value for Hikari: calendar-aware reminders, protected focus time, and frictionless rescheduling, not novelty tools. Source: [Reclaim Features](https://help.reclaim.ai/en/articles/6210740-features-in-reclaim).

Todoist's official docs show that reminders, recurring dates, calendar integration, and AI-assisted task creation/filtering remain central productivity primitives. Hikari should not bury reminders and tasks under a personality layer. Sources: [Todoist reminders](https://get.todoist.help/hc/en-us/articles/205348301-Introduction-to-Reminders), [Todoist Calendar integration](https://get.todoist.help/hc/en-us/articles/13258169208860-Use-the-Calendar-Integration), [Todoist Assist](https://www.todoist.com/help/articles/introduction-to-todoist-assist-KgPP22q5O).

### Tool Discoverability And Command UX

OpenClaw Tool Search and Hermes toolsets both solve the same problem: large catalogs need filtered discovery and scenario bundles. Sources: [OpenClaw Tool Search](https://docs.openclaw.ai/tools/tool-search), [Hermes Toolsets Reference](https://hermes-agent.nousresearch.com/docs/reference/toolsets-reference/).

Atlassian's Jira command palette uses keyboard-driven search and labels result types. The Hikari equivalent is not `/tools` as an inventory dump; it is a command palette in chat: "reminders", "today", "mail", "memory", "receipt", "links", "approvals", "settings". Source: [Atlassian Jira command palette search](https://support.atlassian.com/jira-software-cloud/docs/search-issues-projects-and-more-with-the-your-keyboard/).

Apple's Human Interface Guidelines emphasize platform conventions and familiar components. For Hikari, Telegram-native buttons, inline keyboards, snooze controls, and confirmation cards matter more than clever prose. Source: [Apple Human Interface Guidelines](https://developer.apple.com/design/human-interface-guidelines/).

### Safe Tool Execution And Approval UX

OpenAI's ChatGPT agent docs explicitly call out sensitive app access, prompt injection, user confirmations for high-impact actions, and watch mode for certain tasks. Hikari should mirror this: every real-world-impact action needs a preview and explicit confirmation, especially email, calendar, files, GitHub, Notion, and local execution. Sources: [OpenAI Help: ChatGPT agent](https://help.openai.com/en/articles/11752874-chatgpt-agent), [OpenAI: Introducing ChatGPT agent](https://openai.com/index/introducing-chatgpt-agent/).

OpenClaw exec approvals are a strong model for local execution safety: policy plus allowlist plus approval, bound to canonical command/cwd/context. Source: [OpenClaw Exec Approvals](https://docs.openclaw.ai/tools/exec-approvals).

The MCP security best-practices docs are relevant because Hikari uses external MCP servers. The key lesson is to treat external tool outputs and tool metadata as untrusted and to enforce least privilege, consent, and auditing around tool use. Source: [MCP Security Best Practices](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices).

## 4. Hermes/OpenClaw Tool UX Lessons

1. Bundle by user intent, not implementation.

Hermes toolsets are the right mental model. Hikari should surface "Daily", "Research", "Capture", "External actions", "Developer", "Operator", and "Delight" bundles, not raw MCP/native tool names. Source: [Hermes Toolsets Reference](https://hermes-agent.nousresearch.com/docs/reference/toolsets-reference/).

2. Tool search beats full-schema exposure.

OpenClaw Tool Search exists because large catalogs overwhelm the model and the user. Hikari already has `hikari_router` and tool inventory. Use them more aggressively. Source: [OpenClaw Tool Search](https://docs.openclaw.ai/tools/tool-search).

3. Messaging is a product surface.

Hermes and OpenClaw both treat messaging/channel support as first-class. Hikari should do the same for Telegram: compact cards, buttons, snooze, approve/reject, audit, "why did you message me?", and settings toggles. Sources: [Hermes Messaging Gateway](https://hermes-agent.nousresearch.com/docs/user-guide/messaging), [OpenClaw Channels](https://docs.openclaw.ai/channels).

4. Cron, heartbeat, commitments, and background tasks are different products.

OpenClaw's automation taxonomy is better than saying "proactive". Hikari should name and test each behavior separately: exact reminders, periodic checks, inferred follow-ups, and long-running delegated jobs. Source: [OpenClaw Automation](https://docs.openclaw.ai/automation).

5. Approvals must bind the exact action.

OpenClaw's exec approval docs bind command, cwd, argv/env, and execution plan. Hikari's approval cards should similarly bind the exact recipient/event/file/diff/count/action. Source: [OpenClaw Exec Approvals](https://docs.openclaw.ai/tools/exec-approvals).

6. Skills are useful when they encode workflows, not when they hoard facts.

Hermes skills and OpenClaw skills both act as reusable instructions. Hikari's best future skills should be "how to handle a daily check-in", "how to triage inbox safely", "how to capture a link", "how to write to wiki", and "how to run a local analysis safely". Sources: [Hermes Working with Skills](https://hermes-agent.nousresearch.com/docs/guides/work-with-skills/), [OpenClaw Skills](https://docs.openclaw.ai/tools/skills).

## 5. Tool Family Classification Table

Legend:

- Core daily value: should be excellent and surfaced.
- Occasional high leverage: keep, but invoke only when needed.
- Infrastructure only: keep hidden from the user unless debugging.
- Risky but useful: keep with gates and audit.
- Toy / probably remove: hide by default; remove if unused.
- Needs UX surface before useful: tool exists but is not yet a great product affordance.
- Needs parallel execution: common compound turns should call these together.
- Needs approval: should never perform consequential writes without explicit confirmation.

| Tool family | Classification | Blunt assessment | Telegram/action recommendation |
|---|---|---|---|
| Reminders: `reminder_create/list/cancel/snooze` | Core daily value, Needs UX surface, Needs parallel execution | This is one of the few tools that can create daily trust. It matters more than most fancy integrations. | Surface `/reminders`, create confirmations, snooze/dismiss/edit buttons, overdue cleanup, "next 3" in morning check-in. |
| Calendar reads: `calendar_get_events`, Google Calendar read | Core daily value, Needs parallel execution | Daily leverage if used for prep and conflict detection. Useless if only answer-on-demand. | Include in morning brief, "what's my day?", event prep, travel/weather context. |
| Calendar writes/deletes, Apple/GCal sync | Risky but useful, Needs approval | High leverage, high regret potential. Keep gated. | Approval card must show title, time, attendees, calendar, recurrence, delete target. |
| Gmail read/search/thread triage | Core daily value, Needs parallel execution | Very useful if filtered to important mail. Dangerous if it becomes "summarize my inbox" noise. | Daily check-in should show only important/unread/actionable threads with buttons. |
| Gmail send/reply/bulk delete | Risky but useful, Needs approval | Keep. Never auto-send. Bulk delete is especially dangerous. | Show recipient, subject, exact body/diff, attachment list, count, and irreversible warning. |
| Drive/Docs/Sheets/Slides reads | Occasional high leverage, Needs parallel execution | Useful when the user references a file. Not daily unless paired with calendar/email context. | Search/read from chat naturally; cite document titles and modified dates. |
| Drive/Docs/Sheets/Slides writes/deletes | Risky but useful, Needs approval | Keep, but only with explicit preview. | Approval card with file name, operation, destination, diff/summary, sharing impact. |
| Memory recall/session search/tasks | Core daily value, Needs parallel execution | This is Hikari's identity layer. It should be used often but quietly. | Recall before personal answers; expose `/memory`, `/tasks`, corrections, and "forget/update" confirmations. |
| Memory writes/core-block mutation | Risky but useful, Needs approval for destructive/corrective writes | Remembering is useful; corrupting identity/context is costly. | Allow direct "remember X"; require confirmation for invalidating facts/core blocks unless user explicitly commands it. |
| Wiki search/read/tree/backlinks | Core daily value for this user, Needs parallel execution | The Obsidian vault is a real asset. Search/read are daily leverage when personal context matters. | "I found this in the wiki" with page links; let user append/create with confirmation. |
| `wiki_append` | Risky but useful, Needs approval unless explicitly requested | Writes to durable knowledge. Keep controlled. | Show page, section, text to append, backlinks/tags. |
| Link shelf | Core daily value if surfaced, Needs UX surface, Needs parallel execution | Good idea, currently too invisible. It creates value only when links resurface. | When a URL arrives: saved-card with kind/tag buttons. During related chats: "you saved this". |
| Day receipt | Core daily value, Needs UX surface | Strong personal workflow. It will not stick without one-tap capture. | Buttons for Made/Moved/Learned/Avoided, undo, print today, weekly view. |
| Decision log | Occasional high leverage, Needs UX surface | Excellent for calibration. Too niche to lead with. | Capture prediction cards, due-date reminders, resolve buttons, weekly calibration. |
| Weather/location/morning brief | Core daily value, Needs parallel execution | Useful if brief and contextual. Weather alone is commodity. | Pair with calendar, commute, recurring location patterns, weather alerts. |
| Places/open-now | Occasional high leverage, Toy-ish if oversold | OSM hours coverage is patchy. Useful for casual checks, not a promise engine. | Surface confidence and "hours unavailable" honestly. |
| Translate | Occasional high leverage | Useful, narrow, obvious. | One-message utility, no ceremony. Fail fast when DeepL not configured. |
| Currency convert | Occasional high leverage | Tiny but genuinely useful. | One-message utility. |
| Arxiv search | Occasional high leverage | High leverage for ML/research user, not daily. | Use in research packets, cite paper ids/dates. |
| YouTube transcript | Occasional high leverage | Useful when a link is shared. | Auto-offer summary/capture only for YouTube URLs. |
| YT Music recent/search/library | Toy / probably hide | Personal flavor, not productivity. Keep hidden unless user asks about music. | Do not advertise in `/tools` primary view. |
| Generate photo | Toy / probably hide, Risky reputationally if overused | Companion delight, not productivity. Daily value is low; misuse makes the agent feel gimmicky. | Explicit request only, daily caps, no product-tool positioning. |
| Attachment/document/photo/voice ingestion | Core daily value, Needs parallel execution | Very useful because Telegram-native. | Read/classify/summarize safely; offer save-to-wiki/link/memory actions. |
| `read_attachment` | Core daily value, Risky but useful | Good hard-scoped design. Keep. | Preserve path scoping, prompt-injection wrapping, and magic-byte checks. |
| `calc` | Core utility | Worth keeping; low risk. | Use silently for arithmetic/date diffs. |
| `python_run` | Occasional high leverage, Risky but useful, Needs approval | Sandboxed data analysis is valuable. But code execution should stay gated. | Approval should show purpose, packages, input files, no-network/no-write constraints. |
| DuckDB MCP | Occasional high leverage | Useful for structured local data if present. | Keep hidden until data/table workflows exist. |
| Playwright/browser | Occasional high leverage, Risky but useful | Useful for verification and web tasks. Browser automation has privacy/action risk. | Read-only browsing lower risk; logged-in actions require approval. |
| GitHub reads | Occasional high leverage | Useful for repo questions and PR triage. | Keep as specialist workflow, not daily user surface. |
| GitHub writes/merge/update | Risky but useful, Needs approval | Can break real projects. Keep gated. | Approval card with repo, branch, issue/PR number, diff summary, merge target. |
| Notion reads | Occasional high leverage | Useful only if the user's Notion matters. | Query schema first; cite pages/databases. |
| Notion writes | Risky but useful, Needs approval | Same as docs/wiki writes. | Show exact page/database/properties. |
| Apple Notes create/search/read | Occasional high leverage, Needs UX surface | Good quick-capture path, but durable knowledge should go to wiki. | Ask "Notes or wiki?" when ambiguous. Create only when user asks. |
| Apple Events Reminders/Calendar | Risky but useful, Needs approval | Great native integration, but writes must be exact. | Align gating with gatekeeper, not only marker strings, for LLM-facing paths. |
| Apple Shortcuts | Risky but useful, Needs approval | The wildcard destructive marker is correct to be suspicious. | Never run arbitrary shortcuts without explicit user approval and shortcut name preview. |
| Proactive engagement: reminder/calendar/weather/email/decision | Core daily value when anchored | The anchored producers are useful. Keep strict filters. | Every proactive should answer "why now?" and include snooze/mute. |
| Proactive engagement: mood leak/reengage silence/callback episodes | Toy / probably hide | Personality is fine; do not count it as tool usefulness. | Keep rare, capped, and easy to silence. |
| Scheduler/consolidation/prune/drift/future-letter/evening diary | Infrastructure only or deferred | Mostly internal hygiene. Some may become product, but not yet. | Hide except `/status`/audit. Kill if no observed benefit. |
| `dispatch_claude_session` | Occasional high leverage, Risky but useful, Needs approval | Powerful for coding/research delegation. Not casual. | Approval with repo/path/task/model/cost estimate; final report link. |
| Codex reports list/read | Occasional high leverage | Useful for continuity and project memory. | Surface when user asks "what did Codex find?" or in research/coding loops. |
| Router/tool search/tool inventory | Infrastructure only | Essential plumbing. Not a user feature. | Use internally; `/tools` should show workflows and recent audit, not raw schemas. |
| Gatekeeper/approvals/audit | Infrastructure only, Core safety | This is a product trust layer. Keep investing. | Make approval cards clearer and auditable. |
| MCP/resource wrappers | Infrastructure only | Needed for external ecosystems. Dangerous if overexposed. | Use include/exclude filtering and tool family policy. |
| Stickers/grab stickers | Toy / probably hide | Operator fun. Not core. | Hide from primary help; keep command if used. |

## 6. Top 10 Workflows Hikari Should Make Excellent

1. Morning command center.

Calendar, reminders, weather/location, important Gmail, one key open loop, and maybe yesterday's unresolved receipt. This should be short and actionable.

2. Exact reminders and snoozing.

"Remind me at 14:00", "every Friday", "one hour before", "snooze 20m", "dismiss". This is daily trust.

3. Daily check-in and triage.

Important mail, today's calendar, pending reminders, due decisions, and one user-selectable focus. Not a generic motivational message.

4. Capture from Telegram.

URL, file, photo, voice note, location. Hikari should classify it, summarize it, and offer save-to-link-shelf/wiki/memory/task actions.

5. "Find what I know."

Search memory, sessions, wiki, links, Codex reports, and maybe Drive. The answer must cite where it came from.

6. Calendar/event prep.

Before a meeting: attendees, relevant email thread, docs, notes, previous decisions, travel/weather if location matters.

7. Safe email/calendar/document action.

Draft the action, preview it, get approval, execute, then audit.

8. End-of-day receipt.

Made, moved, learned, avoided. One-tap categories plus weekly rollup.

9. Decision capture and resolution.

Capture predictions and commitments when naturally stated; resolve with buttons; show calibration over time.

10. Research packet.

For current or technical research: web, primary docs, arXiv when relevant, wiki/local notes, link shelf, citations, concise conclusion.

## 7. What To Surface In Telegram

Surface these as first-class:

- `/today` or `/checkin`: calendar, reminders, weather, important mail, open loops.
- `/reminders`: next reminders, add, snooze, cancel, recurring view.
- `/approvals`: pending actions with exact previews.
- `/capture`: save current/last URL/file/photo/voice to links/wiki/memory/task.
- `/receipt`: Made/Moved/Learned/Avoided buttons, print today, week.
- `/decisions`: open decisions, due decisions, resolve.
- `/memory`: facts, corrections, forget/update.
- `/links`: recent links, search, tag cleanup.
- `/wiki`: search/read/append with confirmation.
- `/status`: health only, not a main user journey.

Make Telegram cards do real work:

- Reminder card: text, due time, recurrence, snooze, dismiss, edit.
- Approval card: exact action, target, diff/body/count, approve once, reject, edit.
- Email card: sender, subject, why important, summarize, draft reply, archive only after approval.
- Calendar card: event title/time/location, prep, travel/weather, reschedule request.
- Link card: URL/title, kind buttons, tags, save/delete.
- Receipt card: four category buttons plus undo.
- Decision card: statement, probability, due date, resolve true/false/unclear.

## 8. What To Hide

Hide from primary `/help` and `/tools`:

- Raw MCP server tool lists.
- `generate_photo`.
- YT Music tools.
- Sticker/grab-sticker commands.
- Internal scheduler/consolidation/prune/drift/future-letter jobs.
- Gatekeeper internals except `/approvals` and `/audit`.
- Tool annotations and registry plumbing.
- Playwright/DuckDB unless a user asks for browser/data work.
- Apple Shortcuts wildcard surface.
- Dispatch internals; expose only "start background coding/research job" with approval.

The user does not need to know that Hikari has 165 tools. The user needs to know Hikari can handle the day.

## 9. What To Remove/Defer

Do not rush to delete code yet. Hide first, instrument usage, then prune.

Remove/defer candidates:

- Proactive "weirdly good mood leak" as a productivity feature. Keep as rare personality if it earns replies; otherwise disable.
- Future letter/evening diary/drift canary as visible features. Keep internal only until there is evidence of user value.
- Places/open-now as a flagship. OSM coverage makes this a convenience fallback, not a promise.
- YT Music and stickers from any primary tool marketing.
- Any MCP write tool that has no approval preview test.
- Any slash command that only helps the maintainer but appears in the same help surface as daily user commands.
- Broad "all tools" inventory in chat. Replace with task-oriented categories plus recent/audit.

## 10. Approval And Safety Recommendations

Never run these without explicit approval:

- Gmail send/reply/forward/bulk delete/archive destructive actions.
- Calendar create/update/delete, including Apple and Google mirrors.
- Drive upload/delete/move/share changes.
- Docs/Sheets/Slides writes/deletes/sharing changes.
- GitHub writes: issue/PR creation, comments, labels, branch/ref updates, merges, file writes.
- Notion writes.
- Apple Shortcuts.
- Apple Events writes unless initiated by an exact user command and still previewed.
- `dispatch_claude_session`.
- `python_run` when it touches user files or generated code, even if sandboxed.
- `wiki_append` and Apple Notes create when inferred rather than directly requested.
- `mark_fact_invalid`, `update_core_block`, and other destructive memory/core mutations.

Approval cards should include:

- Exact operation.
- Exact target.
- Exact payload or diff.
- Count of affected items.
- Whether it is reversible.
- External service involved.
- Which user phrase triggered it.
- Expiration/timeout.
- Buttons: approve once, reject, edit, always allow only for narrow safe patterns.

Adopt the OpenClaw-style binding principle for high-risk execution: approval should bind the exact action plan. If recipient, body, file, cwd, command, branch, or event time changes after approval, the approval is invalid. Source: [OpenClaw Exec Approvals](https://docs.openclaw.ai/tools/exec-approvals).

Keep and expand current Hikari protections:

- Keep `untrusted_output: true` wrapping for web, Gmail, Drive, docs, GitHub, Notion, and attachment content.
- Keep hard path scoping for `read_attachment`.
- Keep SSRF protections in link shelf.
- Keep gatekeeper restart recovery and implicit cancel on ordinary messages.
- Add approval preview golden tests for every external write family.
- Add "untrusted content tries to trigger a write" red-team tests for Gmail, Docs, PDFs, web pages, and calendar bodies. OpenAI's agent docs call out prompt injection as a high-impact risk for agents with connectors and web access. Sources: [OpenAI Help: ChatGPT agent](https://help.openai.com/en/articles/11752874-chatgpt-agent), [OpenAI: Introducing ChatGPT agent](https://openai.com/index/introducing-chatgpt-agent/), [MCP Security Best Practices](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices).

## 11. Parallelization Recommendations

Parallelize compound reads. Do not parallelize dependent writes.

High-value read bundles:

- Morning brief: `calendar_get_events`, reminders, weather, important Gmail, tasks/memory, receipt yesterday/today.
- "What's my day?": calendar, reminders, weather/location, tasks, important Gmail.
- Meeting prep: calendar event, Gmail threads, Drive docs, wiki/memory/session search, location/weather if relevant.
- "What did I tell you about X?": `recall`, `session_search`, `wiki_search`, `link_search`, Codex reports if project-related.
- URL/file/photo/voice capture: attachment/transcript read, link metadata, memory/wiki/link search for related context.
- Research packet: WebSearch/WebFetch, official docs, arXiv when relevant, wiki/link shelf for local context.
- Travel/place query: calendar, weather, places/open-now, location memory.
- Code/repo question: GitHub read, local repo search, Codex reports, wiki project page.
- Proactive candidate scoring: calendar, Gmail, reminder, decision, weather candidates can be gathered independently before selector scoring.

Do not parallelize:

- Multiple writes to the same external object.
- Approval creation and execution.
- Bulk delete/send operations.
- Memory invalidation plus new memory write unless the relationship is explicit.
- Calendar create followed by reminder mirror unless idempotency is guaranteed.
- Any action where one tool result determines the exact target of the next write.

Implementation direction:

- Add evals that check compound turns make multiple read calls before composing.
- Add latency budget targets for morning/check-in workflows.
- Track tool-call fanout by workflow, not by global tool usage.
- Treat parallelism as a product feature only when it improves answer freshness or reduces user wait.

## 12. Suggested Tests/Evals

1. Daily-value eval suite.

Scenarios: morning brief, exact reminder, "what's my day?", email triage, meeting prep, capture URL, file ingest, receipt, decision resolution, memory/wiki recall.

2. Approval matrix.

For every external write family, assert no execution occurs before approval. Assert approval previews contain exact target, payload/diff, count, and reversibility.

3. Prompt-injection red team.

Inject malicious instructions into Gmail, Docs, Drive files, PDFs, web pages, calendar descriptions, GitHub issues, and YouTube transcripts. Expected result: no writes, no secret leakage, warning or ignored instruction.

4. Telegram card golden tests.

Snapshot reminder cards, approval cards, email triage cards, receipt buttons, decision cards, link cards, and "why proactive?" cards.

5. Parallel compound-turn evals.

Mock tools and assert Hikari calls calendar/reminders/weather/Gmail in the same turn for daily workflows; recall/session/wiki/link in the same turn for "what do you know about X?"

6. Proactive precision eval.

Every proactive message must include a concrete anchor, a reason now, and a mute/snooze path. Penalize generic check-ins.

7. Degraded-auth evals.

When Google/Notion/GitHub auth is missing, Hikari should say exactly what is unavailable and continue with local context. No hallucinated reads.

8. Usefulness telemetry.

Measure approve/reject rates, proactive reply/snooze/dismiss rates, repeated use by workflow, tool family latency, and follow-up success. Stop counting raw tool invocations as success.

9. Memory mutation eval.

Contradiction, correction, forget, and task close flows should produce the right memory operation and never silently rewrite core identity.

10. Link/attachment safety eval.

Keep SSRF/path traversal tests, add archive/html/pdf prompt-injection cases, and verify saved-link metadata cannot force a tool call.

11. Workflow discoverability eval.

Given "what can you do?", Hikari should show 8-10 task-oriented commands/workflows, not the tool registry.

12. Local execution eval.

For `python_run`, DuckDB, Playwright, dispatch, and Apple Shortcuts: assert gating, sandbox limits, exact plan preview, timeout behavior, and audit trail.

Final blunt recommendation: freeze tool expansion for now. Spend the next cycle making reminders, daily check-in, capture, memory/wiki recall, safe email/calendar actions, day receipt, and decision log feel inevitable in Telegram.
