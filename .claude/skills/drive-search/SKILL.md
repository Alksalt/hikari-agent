---
name: drive-search
description: Search the user's Google Drive / Sheets / Gmail when they reference a document, note, file, or message they think is in there. Wraps the google_workspace MCP server. Requires the user's service-account JSON to be configured in .env (see README). If the MCP isn't connected, fall back to "i can't get into your drive right now."
---

# Drive Search Skill

This skill is a thin wrapper around the `google_workspace` MCP server. Use when the user mentions:

- "my notes about X" / "the doc I sent you" / "the spreadsheet"
- "did I email <person> about Y"
- "find the meeting notes from <date>"

## Available tools (when MCP is connected)

The `google-workspace-mcp` server exposes (subject to your OAuth scope grants):

- `mcp__google_workspace__drive_search_files(query, page_size, ...)` — full-text Drive search
- `mcp__google_workspace__drive_read_file_content(file_id)` — fetch contents
- `mcp__google_workspace__sheets_read_range(spreadsheet_id, range_a1)` — read a sheet range
- `mcp__google_workspace__query_gmail_emails(query, max_results)` — Gmail search query
- `mcp__google_workspace__gmail_get_message_details(email_id)` — read one email
- `mcp__google_workspace__calendar_get_events(time_min, time_max, calendar_id)` — list events
- `mcp__google_workspace__create_calendar_event(summary, start_time, end_time, ...)` — create event
- `mcp__google_workspace__create_gmail_draft(to, subject, body, ...)` — draft email
- `mcp__google_workspace__docs_get_content_as_markdown(document_id)` — read a doc
- Plus full Docs/Sheets/Slides write tools — list available tools if uncertain.

## How to call

Phrase the query the way the user phrased it. Don't paraphrase.

```
mcp__google_workspace__drive_search_files(query="meeting notes Q2 product review", page_size=10)
```

Return at most the top 3 results in Hikari's voice. Never paste raw JSON.

## When MCP is not connected

If the server isn't configured (likely if the user just set up the bot), respond in character without trying:

- "i can't see your drive. you didn't wire that up yet."
- "drive's not connected. you want to fix that or am i guessing?"

## Don't

- Don't read entire long docs; pull headings / first paragraph.
- Don't auto-summarize. Quote the line that actually answers, not the whole doc.
- Don't browse the user's email beyond what they asked. She isn't snooping.
