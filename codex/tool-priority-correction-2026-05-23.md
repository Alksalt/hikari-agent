---
title: Tool Priority Correction
date: 2026-05-23
repo: /Users/ol/agents/hikari-agent
reviewer: Codex
extends:
  - codex/tool-surface-google-hermes-openclaw-2026-05-23.md
  - codex/tool-surface-deep-dive-2026-05-23.md
---

# Tool Priority Correction

## Short Answer

The previous deep dive focused too much on Google because the original ask
explicitly said "google" and because Google is the sharpest existing risk
surface: Gmail, Calendar, Drive, Docs, Sheets, and Slides already sit behind a
wildcard MCP grant.

That was a safety-first framing, not the right product priority framing.

Corrected position:

- **Google is P0 only for safety/policy cleanup.**
- **Google is not the highest-value new tool family.**
- The highest-value next work is closer to Hikari's core companion loop:
  memory/session search, tool/status visibility, reading pipeline, routines,
  code/project workflow, and shift/life logistics.

## Corrected Priority Stack

### P0 - Tool Trust, Status, And Audit

This is not a new external connector, but it is the highest-priority tool work.

Why:

- Hikari already has enough power to affect email, calendar, files, notes,
  reminders, memory, links, and background coding tasks.
- The user currently cannot easily see "what tools are connected?", "what just
  ran?", "what failed?", or "what is waiting for approval?"
- External wildcard grants can drift.

Build:

- `/tools` - connected tool families, auth status, gate policy.
- `/status` - scheduler, Google health, MCP envs, DB, live SDK session,
  pending approvals, silence/proactive state.
- `/audit` - recent tool calls, external writes, approvals, failures.
- `tool_audit_recent` and `approval_recent` read-only utility tools.
- Registry validator improvements for wildcard write drift.

This beats adding new Google features because it makes every existing feature
safer and more understandable.

### P1 - Memory And Session Search

This is more important than Google Tasks or Contacts.

Why:

- The core product is a companion with continuity.
- Hikari has facts, episodes, messages, observations, peer model, and core
  blocks, but the user-facing retrieval controls are thin.
- Hermes has visible session search; Hikari should have a local, more personal
  version.

Build:

- `session_search(query, limit, since)` over final-sent messages.
- `session_recent(limit, source)` for recent visible context.
- `/memory` inspect/correct/forget commands.
- "why did you remember that?" provenance.
- Fact source ids and source snippets.

This is one of the highest-leverage improvements because it makes Hikari feel
less like a bot with tools and more like someone with a trustworthy memory.

### P1 - Readwise / Reader And Reading Pipeline

This is probably a higher-value new connector than more Google APIs.

Why:

- README already mentions `READWISE_TOKEN`, but there is no local tool surface
  in the current inventory.
- The user's interests lean heavily toward AI news, psychology, tech, and
  reading.
- Readwise Reader is a natural bridge into the wiki, link shelf, briefings, and
  memory.

Readwise Reader API supports document save, list, update, bulk update, delete,
and tag list. Documents include categories such as article, email, RSS,
highlight, note, PDF, EPUB, tweet, and video. The list endpoint supports
filters such as `updatedAfter`, `location`, `category`, `tag`, pagination, and
optional HTML content. Source: https://readwise.io/reader_api

Build first:

- `readwise_reader_list(location="later|feed|archive", category=null, tag=null, limit=20)`
- `readwise_reader_search(query/tag/category)` if API/filtering is enough;
  otherwise local cache + search.
- `readwise_highlights_recent(limit=20, updated_after=null)`
- `readwise_save_url(url, tags=[], notes=null)` gated or explicit.
- `readwise_to_wiki(document_id)` as a controlled ingest path.

Gate:

- reads: no gate, untrusted-wrapped.
- save/update/archive/tag: `defer`.
- delete/bulk update: `gatekeeper`.

This should integrate with `link_shelf` and wiki filing, not become a separate
island.

### P1 - Briefing And Attention Sources

This is more Hikari-shaped than Google Contacts.

Why:

- The wiki already contains AI/noise/vibecode briefings.
- The user follows AI, gaming, tech, and project/tooling news.
- OpenClaw's most relevant routine idea is not Google; it is configurable brief
  sources: Hacker News, world news, weather, stocks/crypto, GitHub trending,
  Reddit.

Build:

- `brief_source_list`
- `brief_source_add`
- `brief_run_now(topic/source)`
- `brief_write_to_wiki`
- Source adapters:
  - Hacker News official Firebase API (public item/top/best/new/user data):
    https://github.com/HackerNews/API
  - GitHub trending/search/release watch via GitHub tools.
  - arXiv already exists; wrap it into a briefing pipeline.
  - Readwise Reader feed/later documents.
  - RSS feeds, if we add a small local RSS reader.

Avoid:

- default news spam.
- proactive sends without cadence and source attribution.
- low-signal social media scraping.

### P1/P2 - Shift And Life Logistics

This may be more valuable than any generic SaaS connector.

Why:

- The user works shifts and uses Visma Flyt Ressursstyring.
- Work schedule affects sleep, training, reminders, daily check-in timing, and
  proactive cadence.
- A schedule-aware companion is more personally useful than another document
  API.

Build options:

- `shift_import_from_screenshot` using the existing Telegram photo/document
  ingestion path plus a typed parser.
- `shift_list`, `shift_next`, `shift_set`, `shift_delete`.
- Calendar/reminder mirror only after typed extraction confirmation.
- Daily check-in windows based on shift schedule.

Gate:

- importing parsed shifts: user confirms parsed table.
- calendar writes: defer.
- local shift ledger writes: allow after explicit "yes/import".

This is a bespoke tool, but it is the kind of bespoke that matters.

### P2 - Coding And Project Workflow

This is more valuable than expanding Google Docs/Slides.

Why:

- The user is actively building Hikari, Meria, and other agent/product systems.
- Hikari already has GitHub and background dispatch.
- OpenClaw's best idea is worker discipline, not Google integrations.

Build:

- stronger `/tasks` for background coding workers;
- `worker_audit_recent`;
- artifact/report linking;
- PR/issue status summaries;
- CI failure triage path;
- explicit notification route for every dispatched worker.

Connector choices:

- GitHub's official MCP server is now GitHub-owned and open source; the
  changelog says it rewrote the reference server in Go and added features such
  as customizable tool descriptions and code scanning support:
  https://github.blog/changelog/2025-04-04-github-mcp-server-public-preview/
- Linear has an official remote MCP server using Streamable HTTP and OAuth 2.1,
  with tools for finding, creating, and updating issues/projects/comments:
  https://linear.app/docs/mcp

Recommendation:

- keep GitHub but review whether to move from the older npm server to the
  official GitHub MCP server;
- add Linear only if the user is actually using Linear for projects;
- gate all write/comment/issue/PR state changes.

### P2 - Link Shelf Hardening And Reader-Style Save Flow

This is already in Hikari and should be upgraded before adding more external
tools.

Why:

- User-shared links are common and personal.
- `link_save` can fetch arbitrary http(s) URLs; previous report noted private
  IP/localhost redirect blocking is missing.
- Link shelf plus Readwise plus wiki could become one coherent "attention
  intake" system.

Build:

- private-network/loopback redirect block;
- content-type and size caps;
- `link_ingest_to_wiki`;
- `link_summarize_for_later`;
- duplicate detection by canonical URL;
- topic tags from current conversation.

### P2 - Apple / Local OS Tool Policy

This is higher priority than more Google write tools because it is already
available and ungated.

Current state:

- Apple Events and Apple Shortcuts are wildcard exposed.
- Apple Notes create is in-process and ungated.
- Reminders can mirror to Apple Reminders and Google Calendar.

Build:

- `/tools apple` status and permission explanation.
- Shortcut inventory/safe-list before allowing arbitrary shortcuts.
- Gate destructive calendar/reminder/shortcut actions if they expand beyond
  low-risk personal clutter.
- Audit all local OS side effects.

The goal is not to make Apple annoying. The goal is to know what local powers
are actually live.

### P3 - Google New Capabilities

This is where Google belongs after the safety patch.

Do:

- explicit write-gating policy for existing Google Workspace tools;
- Contacts read-only for email/calendar disambiguation;
- Tasks mirror if it connects to reminders/open loops;
- selected `gws` helper workflows only behind a narrow wrapper.

Do not:

- expose raw `gws`;
- add Workspace Events before polling/routines are stable;
- add Admin/Classroom/Vault/Chat unless a real need appears.

### P3 - Media, Music, And Personal Taste Tools

Nice, but not core yet.

Existing:

- YouTube Music recent/search/library.
- Photo generation.
- Voice transcription.

Possible:

- Spotify/Last.fm only if YouTube Music is insufficient.
- anime/gaming backlog/watchlist if the user wants Hikari to track taste.
- movie/game release watch as a brief source, not a chat-time tool.

### P4 - Finance / Stocks / Crypto

OpenClaw includes stocks/crypto briefs, but for Hikari this should stay opt-in.

Reason:

- If the user is not actively asking for market monitoring, it is noise.
- Financial tool outputs need high accuracy, source attribution, and careful
  framing.

## Revised Ranking

| Rank | Work | Why It Beats Google Expansion |
|---:|---|---|
| 1 | `/status`, `/tools`, `/audit`, registry validation | Makes every current tool safer and visible |
| 2 | Memory/session search and correction | Core companion capability |
| 3 | Readwise/Reader + link/wiki intake | Matches user's reading/AI-news workflow |
| 4 | Brief source pipeline | Turns existing briefings into a real product loop |
| 5 | Shift/life logistics | Personally useful, cadence-aware, not generic SaaS |
| 6 | Coding/project workflow hardening | Matches current building-heavy life |
| 7 | Link shelf hardening | Existing useful surface needs safety and polish |
| 8 | Apple/local OS policy | Already live and powerful |
| 9 | Google safety patch | Necessary, but mostly risk reduction |
| 10 | Google Tasks/Contacts/`gws_helper` | Useful after the policy cleanup |

## What I Would Do Next

If choosing one implementation sequence:

1. Build `/status` + `/tools` + read-only audit tools.
2. Add `session_search`.
3. Add Readwise Reader list/search/save and wire it to link shelf/wiki.
4. Add configurable brief sources using existing arXiv/wiki plus HN/Readwise.
5. Add shift schedule import/ledger.
6. Then do Google write-gating cleanup.

If choosing one safety sequence:

1. Google and external wildcard write policy.
2. Apple Shortcuts safe-list/audit.
3. Link shelf SSRF hardening.
4. Approval preview improvements.
5. External MCP package pinning.

## Bottom Line

The better framing is:

- Google is the biggest **existing-risk cleanup**.
- Memory/search/status/Readwise/briefs/shifts are bigger **product upgrades**.
- Coding/project workflow is the bigger **builder leverage** upgrade.

So yes: other tools are more priority. Google only looked dominant because the
question and the current risk surface pulled the research there. The roadmap
should not.
