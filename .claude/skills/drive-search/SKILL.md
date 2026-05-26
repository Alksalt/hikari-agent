---
name: drive-search
description: Search the user's Google Drive / Sheets / Docs / Gmail / Calendar when they reference a document, file, spreadsheet, email, or event. Wraps the google_workspace MCP server. If the MCP isn't connected or OAuth scope is missing, report the literal error in voice — do not invent OAuth steps or click-Allow instructions.
---

# Drive Search Skill

Thin wrapper around the `google_workspace` MCP server. Use when the user mentions:

- "my notes about X" / "the doc I sent you" / "the spreadsheet"
- "did I email <person> about Y" / "find the email from <date>"
- "what's on my calendar this week" / "find the meeting notes"
- "is there a file about X in my drive"

## Tool names (current — google-workspace-mcp 2.0.x)

### Drive
```
mcp__google_workspace__drive_search_files(query, page_size)
mcp__google_workspace__drive_read_file_content(file_id)
mcp__google_workspace__drive_list_shared_drives()
mcp__google_workspace__drive_create_folder(name, parent_id)
mcp__google_workspace__drive_upload_file(name, content, mime_type, parent_id)
mcp__google_workspace__drive_delete_file(file_id)
```

### Docs
```
mcp__google_workspace__docs_get_content_as_markdown(document_id)
mcp__google_workspace__docs_get_document_metadata(document_id)
mcp__google_workspace__docs_create_document(title)
mcp__google_workspace__docs_append_text(document_id, text)
mcp__google_workspace__docs_prepend_text(document_id, text)
mcp__google_workspace__docs_insert_text(document_id, text, index)
mcp__google_workspace__docs_insert_image(document_id, image_url, index)
mcp__google_workspace__docs_batch_update(document_id, requests)
```

### Sheets
```
mcp__google_workspace__sheets_read_range(spreadsheet_id, range_a1)
mcp__google_workspace__sheets_write_range(spreadsheet_id, range_a1, values)
mcp__google_workspace__sheets_append_rows(spreadsheet_id, range_a1, values)
mcp__google_workspace__sheets_clear_range(spreadsheet_id, range_a1)
mcp__google_workspace__sheets_create_spreadsheet(title)
mcp__google_workspace__sheets_add_sheet(spreadsheet_id, title)
mcp__google_workspace__sheets_delete_sheet(spreadsheet_id, sheet_id)
```

### Gmail
```
mcp__google_workspace__query_gmail_emails(query, max_results)
mcp__google_workspace__gmail_get_message_details(email_id)
mcp__google_workspace__create_gmail_draft(to, subject, body)
mcp__google_workspace__gmail_send_draft(draft_id)
mcp__google_workspace__gmail_send_email(to, subject, body)
mcp__google_workspace__gmail_reply_to_email(message_id, body)
mcp__google_workspace__gmail_get_attachment_content(message_id, attachment_id)
mcp__google_workspace__gmail_bulk_delete_messages(query)
```

### Calendar
```
mcp__google_workspace__calendar_get_events(time_min, time_max, calendar_id)
mcp__google_workspace__calendar_get_event_details(event_id, calendar_id)
mcp__google_workspace__create_calendar_event(summary, start_time, end_time, description, attendees)
mcp__google_workspace__delete_calendar_event(event_id, calendar_id)
```

### Slides
```
mcp__google_workspace__create_presentation(title)
mcp__google_workspace__get_presentation(presentation_id)
mcp__google_workspace__get_slides(presentation_id)
mcp__google_workspace__create_slide(presentation_id, layout)
mcp__google_workspace__duplicate_slide(presentation_id, slide_id)
mcp__google_workspace__delete_slide(presentation_id, slide_id)
mcp__google_workspace__add_text_to_slide(presentation_id, slide_id, text, ...)
mcp__google_workspace__add_table_to_slide(presentation_id, slide_id, rows, cols)
```

## How to call

Phrase the query the way the user phrased it. Don't paraphrase or over-clean.

```python
mcp__google_workspace__drive_search_files(query="meeting notes Q2 product review", page_size=10)
```

Return at most top 3 results in Hikari's voice. Never paste raw JSON.

For docs: don't read the entire document. Pull headings / first section. Quote the line that actually answers, not the whole doc.

For Gmail: don't browse beyond what was asked. She isn't snooping. Get the thread that matches, read the relevant message, done.

## Write operations — gated

All write ops (draft, send, create event, delete) require CONFIRM-SEND. Surface it in voice first:
- "i found the thread. want me to draft a reply?"
- "i can create that event — confirm?"

Then call `dispatch_claude_session` if the user says yes. The bridge prompts CONFIRM-SEND in telegram chat. Don't fire blind.

## OAuth errors — literal reporting

When a tool call fails with a scope error, report what the tool actually said:
- "gmail says insufficient scope — the google OAuth grant doesn't cover that action."
- "drive can't reach that file — service account probably doesn't have access."

Do NOT invent steps like "go to Google Console and add the scope" or "click Allow on the prompt." There is no UI prompt at runtime. The fix is a backend config change.

## When MCP is not connected

- "i can't see your drive. you didn't wire that up yet."
- "drive's not connected. want to fix that or am i guessing?"
