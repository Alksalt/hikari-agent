---
title: Other Tools Review
date: 2026-05-23
repo: /Users/ol/agents/hikari-agent
reviewer: Codex
---

# Other Tools Review

## Executive Take

Non-Google tools matter more for Hikari's product direction than additional
Google tools. Google is mostly a safety cleanup because it is already wired and
has broad write risk. The highest-value non-Google tools are the ones that make
Hikari more legible, more continuous, and more useful in the user's real daily
loop.

Correct priority:

1. Tool/status/audit visibility.
2. Memory and session search.
3. Readwise/Reader and link/wiki intake.
4. Briefing and attention sources.
5. Shift/life logistics.
6. Coding/project workflow.
7. Apple/local OS policy.
8. Link shelf hardening.
9. Optional media/taste tools.
10. Google expansion.

## Scoring Frame

Each tool family is scored by:

- **Companion value**: does it make Hikari feel more continuous/useful?
- **Owner fit**: does it match the user's actual life and projects?
- **Safety**: can it be exposed without making Hikari reckless?
- **Implementation cost**: can this be done with current architecture?
- **Existing leverage**: does Hikari already have half of it?

The important distinction:

- **Connector**: another external app/API.
- **Capability**: a Hikari behavior that may use several tools underneath.

Hikari needs more capabilities, not just more connectors.

## Tier 0 - Visibility And Trust Tools

### 1. `/status`, `/tools`, `/audit`

Priority: highest.

This is the most important "other tool" work because Hikari already has a
large hidden tool surface. A companion that can act on external systems needs
to be explainable from Telegram.

Build:

- `/status`
  - bot process up/down
  - scheduler jobs
  - silence mode
  - last proactive send
  - pending approvals
  - Google/Notion/GitHub auth state
  - external MCP server env status
  - DB path and backup freshness
- `/tools`
  - connected tool families
  - auth configured/unconfigured
  - read/write/destructive classification
  - what requires confirmation
- `/audit`
  - recent tool calls
  - recent external writes
  - recent failures
  - pending and resolved approvals

Tool candidates:

```text
tool_inventory_read()
tool_audit_recent(limit=20, filter=null)
approval_recent(limit=10)
status_snapshot()
```

Why it beats adding connectors:

- It makes every existing connector safer.
- It reduces "what can she do?" confusion.
- It supports debugging without reading logs.

Implementation:

- Mostly local SQLite/runtime reads.
- No external auth.
- Low risk.

### 2. Registry safety tools

Priority: highest safety companion to `/tools`.

Build:

- `tool_policy_validate_live()`
- `tool_policy_diff_upstream(server)`
- write-like wildcard detection
- external package version snapshot

Why:

- Current wildcard MCP grants can add new upstream tools without review.
- This is broader than Google: Notion, GitHub, Playwright, Apple Events,
  Apple Shortcuts, DuckDB, and future MCPs all need drift detection.

## Tier 1 - Memory And Continuity Tools

### 3. Session search

Priority: very high.

Hikari has memory facts, episodes, observations, peer model, and messages. But
the user-facing search surface is still thin. "What did we say about X?" should
not be answered through vague recall alone.

Build:

```text
session_search(query, limit=8, since=null)
session_recent(limit=20, source=null)
conversation_sources_for_fact(fact_id)
```

Product behavior:

- Return snippets with timestamp, role, source, and message id.
- Distinguish raw transcript from curated memory.
- Wrap output as untrusted because transcripts can contain web/email/file text.

Why:

- This is a core companion capability.
- It makes memory inspectable.
- It helps debug why Hikari believes something.

### 4. Memory inspection/correction commands

Priority: very high.

Build:

- `/memory search <query>`
- `/memory forget <id>`
- `/memory correct <id>`
- `/memory open_loops`
- `/memory why <topic>`

Tool candidates:

```text
memory_search_private(query, limit=10)
memory_fact_get(id)
memory_fact_correct(id, new_text)
memory_fact_forget(id)
memory_open_loops()
```

Current tools already cover some primitives (`recall`, `remember`,
`mark_fact_invalid`, `task_update`), but the UX is not productized.

Why it beats Google:

- Google helps Hikari access data.
- Memory tools help Hikari be trustworthy.

## Tier 2 - Reading, Links, And Knowledge Intake

### 5. Readwise / Reader

Priority: very high.

This is the strongest non-Google external tool candidate.

Why:

- README already mentions `READWISE_TOKEN`, but no current local tool surface
  exists.
- The user's reading/research life is central: AI news, psychology, tech,
  project research, and saved articles.
- Readwise/Reader naturally connects to link shelf, wiki, briefings, and memory.
- As of 2026, Readwise has API, CLI, MCP, and skills surfaces.

Useful current sources:

- Reader API: https://readwise.io/reader_api
- Readwise API details: https://readwise.io/api_deets
- Readwise changelog announcing MCP/CLI/skills: https://docs.readwise.io/changelog

Implementation options:

| Option | Pros | Cons | Recommendation |
|---|---|---|---|
| Direct API tools | Small, testable, policy-controlled | Build search/cache ourselves | Best first implementation |
| Official Readwise MCP | Fastest broad access, OAuth/MCP-native | Larger external surface, need tool policy review | Evaluate after API MVP |
| Readwise CLI | Useful for dispatch/coding contexts | Shell wrapper needs strict allowlist | Good for background workflows |

MVP tools:

```text
readwise_reader_list(location="later", category=null, tag=null, limit=20)
readwise_reader_get(document_id, with_html=false)
readwise_reader_search(query, limit=10)
readwise_save_url(url, tags=[], notes=null)
readwise_highlights_recent(limit=20)
readwise_to_wiki(document_id, target_path=null)
```

Gates:

- Reads: no gate, wrap as untrusted.
- Save/update/archive/tag: defer.
- Delete/bulk update: gatekeeper.

Integration:

- Save shared URLs into both link shelf and Reader when appropriate.
- Let Hikari ask "save to Reader?" for high-value links.
- `readwise_to_wiki` should follow vault conventions and use the wiki path
  discipline.

### 6. Link shelf upgrade

Priority: high.

Existing tools:

- `link_save`
- `link_search`
- `link_list`
- `link_update`
- `link_delete`

Needed:

- private-network/loopback redirect block;
- duplicate canonicalization;
- content-type and max-size checks;
- source trust labels;
- `link_ingest_to_wiki`;
- `link_send_to_readwise`;
- better tag suggestions from conversation context.

Why:

- The link shelf is already the seed of a personal research inbox.
- It should become the lightweight default intake path, with Readwise for
  deeper reading and the wiki for durable synthesis.

### 7. Wiki filing tools

Priority: high, but local.

Existing:

- wiki search/read/append/list/tree/backlinks.

Missing:

- safe create/update with frontmatter discipline;
- `updated:` maintenance;
- index update helper;
- raw source filing helper;
- log append helper;
- "ingest this link/document to wiki" workflow.

Build:

```text
wiki_upsert_page(path, title, body, frontmatter, update_index=true)
wiki_file_source(raw_path_or_url, target_page, summary)
wiki_update_index(path, description)
wiki_log_operation(action, paths)
```

Why:

- The wiki is the user's long-term source of truth.
- A general `wiki_append` is not enough for clean knowledge management.

## Tier 3 - Briefing And Attention Tools

### 8. Brief source registry

Priority: high.

Hikari already has AI/noise/vibecode briefings in the wiki and arXiv search.
What is missing is a typed source registry and repeatable briefing pipeline.

Build:

```text
brief_source_list()
brief_source_add(kind, config)
brief_source_disable(id)
brief_run_now(source_or_topic)
brief_write_to_wiki(topic, date)
```

Candidate sources:

- Readwise Reader feed/later.
- arXiv (already present).
- Hacker News official Firebase API: https://github.com/HackerNews/API
- GitHub releases/search via GitHub tools.
- RSS feeds via a small local parser.
- YouTube transcript MCP for selected video URLs.

Policy:

- Read-only by default.
- Source attribution required.
- No automatic proactive send unless a source is explicitly enabled for that.

### 9. Gaming/anime/media watchlists

Priority: medium.

This fits the user's interests but should not outrank memory/reading/status.

Potential integrations:

- AniList API for anime/manga tracking.
- IGDB/rawg/Steam for game release tracking.
- YouTube Music is already present.
- Trakt/TMDb only if the user asks for film/TV tracking.

Better first version:

- local `taste_watchlist` in SQLite;
- manual add/search/update;
- optional source adapters later.

Build:

```text
taste_watch_add(kind, title, status, notes=null)
taste_watch_search(query, kind=null)
taste_watch_update(id, status, rating=null, notes=null)
```

Why local first:

- Avoids noisy OAuth setup.
- Captures preferences without trusting a third-party tracker.

## Tier 4 - Life Logistics Tools

### 10. Shift schedule tools

Priority: high if we optimize for personal usefulness.

The user works shifts and uses Visma Flyt Ressursstyring. Even without a formal
API, a screenshot/document parser may be enough.

Build:

```text
shift_import_from_attachment(attachment_id)
shift_preview_import(parsed_rows)
shift_confirm_import(batch_id)
shift_next()
shift_week(date=null)
shift_delete(id)
```

Workflow:

1. User uploads schedule screenshot/PDF.
2. Hikari extracts candidate rows.
3. Hikari shows a concise preview.
4. User confirms.
5. Local shift ledger updates.
6. Optional calendar/reminder mirrors defer-gated.

Why:

- Shifts drive sleep, training, commute, daily check-in, and proactive timing.
- This is much more personal than another generic SaaS tool.

### 11. Health / fitness logs

Priority: medium.

Do not start with Apple Health unless there is a strong reason; HealthKit on
macOS/Telegram has friction and privacy implications.

Better:

- local workout/yoga/hike log;
- import from screenshots/messages;
- lightweight weekly trend.

Build:

```text
fitness_log_add(kind, date, duration, notes=null)
fitness_log_week()
fitness_goal_set(metric, target)
```

This belongs after memory/status/reading unless the user explicitly prioritizes
health tracking.

## Tier 5 - Builder And Project Tools

### 12. GitHub upgrade and CI triage

Priority: high for builder leverage.

Current state:

- Hikari has GitHub MCP wildcard and a `github` subagent.
- Writes like create issue/PR/merge/delete are gated.
- The external package is old-style npm `@modelcontextprotocol/server-github`.

Candidate upgrade:

- GitHub now has an official open-source local MCP server in public preview,
  rewritten in Go and maintained by GitHub:
  https://github.blog/changelog/2025-04-04-github-mcp-server-public-preview/

Build:

- evaluate official server vs current npm server;
- snapshot tool names;
- gate issue/PR/comment/merge/delete/update operations;
- add CI failure summarizer;
- add PR status/watch tools if not covered by current GitHub app/plugin.

Hikari-specific capability:

```text
github_ci_triage(repo, pr_or_branch)
github_pr_summary(repo, pr)
github_recent_activity(repo, limit=20)
```

### 13. Linear MCP

Priority: conditional.

Linear has an official remote MCP server with Streamable HTTP/OAuth 2.1 and
tools for project/issue/comment operations: https://linear.app/docs/mcp

Add only if the user's actual project management lives in Linear. Otherwise it
is another shiny connector.

Policy:

- reads: no gate, wrapped;
- issue/comment/project writes: defer;
- deletes/status bulk changes: gatekeeper.

### 14. Background worker observability

Priority: high if dispatch is used often.

Current:

- `dispatch_claude_session`
- background tasks table
- `/tasks`

Needed:

- worker stdout/stderr tail;
- final artifact links;
- cost/runtime summary;
- "failed because" message;
- explicit completion notifications;
- cancel/retry per task.

Tool candidates:

```text
worker_task_recent(limit=10)
worker_task_get(id)
worker_task_tail(id, lines=80)
worker_task_cancel(id)
worker_task_retry(id)
```

This is the OpenClaw lesson worth copying.

## Tier 6 - Local OS And Automation

### 15. Apple Shortcuts safe-list

Priority: medium-high safety.

Current:

- Apple Shortcuts MCP is wildcard exposed.
- A user-authored Shortcut can do almost anything.

Build:

- `shortcuts_list_safe()`
- local allowlist by shortcut name/id;
- gate unknown shortcuts;
- audit every run;
- surface shortcut outputs as untrusted.

This is not a feature expansion. It is a power limiter.

### 16. Apple Notes policy

Priority: medium.

Current:

- `note_create`, `note_search`, `note_read`.

Needed:

- note creation audit;
- maybe a dedicated capture note/folder;
- avoid using Apple Notes as a shadow wiki.

Recommendation:

- Keep Apple Notes as sticky capture only.
- Durable knowledge goes to wiki.

## Tier 7 - Communication And Social Tools

### 17. Telegram UX tools

Priority: high, because Telegram is the product surface.

Build:

- inline buttons for common approvals;
- richer `/help`;
- settings toggles;
- tool cards/status summaries;
- quick "save/read later/log/forget" actions.

This is not a new connector, but it probably beats Slack/Discord.

### 18. Slack / Discord

Priority: low unless a concrete workflow appears.

Why:

- Hikari is single-user Telegram-first.
- More inbound surfaces increase context and privacy complexity.
- Hermes supports broad messaging, but Hikari does not need to copy that.

Add only for:

- project community monitoring;
- team notification routing;
- specific server/channel workflows.

### 19. Social posting / creator workflow

Priority: medium later.

The user has mentioned TikTok/Reels/content self-promotion plans, but direct
posting tools are risky. Start with draft/planning, not publishing.

Build:

```text
content_idea_capture(platform, hook, notes)
content_script_draft(topic, style)
content_calendar_list()
content_calendar_schedule_draft(date, platform, idea_id)
```

Gate:

- publishing: do not implement initially.
- calendar scheduling: defer.

## Tier 8 - Analytics And Data Tools

### 20. DuckDB analytics layer

Priority: medium.

Current:

- DuckDB MCP is configured for in-memory analytics with read-only SQLite attach
  by convention.

More useful than adding new connectors:

- typed analytics helpers for common questions;
- receipt trends;
- message/proactive cadence stats;
- memory recall stats;
- cost/tool usage stats.

Build:

```text
analytics_receipts_week()
analytics_tool_calls(days=7)
analytics_memory_recall(days=30)
analytics_proactive(days=30)
```

This avoids asking the model to write SQL every time.

### 21. Finance

Priority: low.

Current:

- currency conversion only.

Add only if requested:

- watchlist;
- portfolio snapshot;
- market/news brief.

Risk:

- financial data needs current sources, source attribution, and careful wording.

## Tool Families To Avoid For Now

Avoid:

- browser shopping/order automation;
- banking/finance mutation;
- direct social posting;
- broad Slack/Discord ingestion;
- arbitrary shell/file access in the main chat loop;
- raw `gws`/raw MCP firehoses;
- unreviewed remote MCP servers with OAuth write scopes;
- health data sync before there is a clear local product shape.

## Recommended Non-Google Roadmap

### Wave A - Legibility

1. `/status`
2. `/tools`
3. `/audit`
4. `tool_audit_recent`
5. `approval_recent`

### Wave B - Memory Product

1. `session_search`
2. `session_recent`
3. `/memory search`
4. `/memory correct`
5. fact provenance

### Wave C - Reading And Briefing

1. Readwise API MVP.
2. Link shelf hardening.
3. Wiki filing helpers.
4. Brief source registry.
5. HN/arXiv/Readwise/RSS adapters.

### Wave D - Personal Logistics

1. shift schedule import;
2. shift ledger;
3. cadence/check-in integration;
4. optional calendar mirrors.

### Wave E - Builder Workflow

1. worker observability;
2. GitHub official MCP evaluation;
3. CI triage;
4. Linear only if actually used.

### Wave F - Local OS Policy

1. Apple Shortcuts safe-list;
2. Apple Notes capture discipline;
3. local side-effect audit.

## Final Ranking

| Rank | Tool/capability | Priority | Why |
|---:|---|---|---|
| 1 | `/status`, `/tools`, `/audit` | P0 | Makes existing power legible |
| 2 | Session search + memory correction | P1 | Core companion continuity |
| 3 | Readwise/Reader | P1 | Best external non-Google connector |
| 4 | Link shelf + wiki filing | P1 | Turns shared URLs into knowledge |
| 5 | Brief source registry | P1 | Makes briefings repeatable |
| 6 | Shift schedule tools | P1/P2 | Highest personal-life fit |
| 7 | Worker/task observability | P2 | Builder leverage |
| 8 | GitHub official MCP / CI triage | P2 | Project workflow leverage |
| 9 | Apple Shortcuts safe-list | P2 | Existing powerful local surface |
| 10 | DuckDB typed analytics helpers | P2/P3 | Useful self-inspection |
| 11 | Linear MCP | Conditional | Only if Linear is real workflow |
| 12 | Media/taste watchlist | P3 | Personal but not urgent |
| 13 | Social content planning | P3 | Useful later, draft-only first |
| 14 | Finance/watchlist | P4 | Opt-in only |
| 15 | More Google tools | P4 after safety | Useful, not core |

## Bottom Line

The best "other tools" are not another broad SaaS connector. They are the
surfaces that make Hikari inspectable, continuous, and personally useful:

- status and audit,
- memory/session search,
- Readwise and link/wiki intake,
- brief sources,
- shift logistics,
- worker/project observability.

Google gets attention because it is already powerful and risky. The roadmap
should put non-Google capability work ahead of new Google expansion.

## Sources

Local sources:

- `codex/tool-priority-correction-2026-05-23.md`
- `codex/tool-subagent-inventory-2026-05-23.md`
- `config/tools.yaml`
- `tools/**`

External sources:

- Readwise Reader API: https://readwise.io/reader_api
- Readwise API details: https://readwise.io/api_deets
- Readwise changelog: https://docs.readwise.io/changelog
- Hacker News official API: https://github.com/HackerNews/API
- GitHub MCP public preview: https://github.blog/changelog/2025-04-04-github-mcp-server-public-preview/
- Linear MCP docs: https://linear.app/docs/mcp
