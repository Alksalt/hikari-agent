# day_receipt

End-of-day log in four fixed bands — `made` (created / shipped),
`moved` (advanced but not done), `learned` (insights), `avoided`
(chose-not-to — also valuable signal). Plus a free-form
top-of-receipt note (mood, weather, one-liner) per date.

Ported in-process from the standalone `day-receipt` MCP server at
`/Users/alt/work_dir/apps/day-receipt`. Behavior is byte-for-byte
identical; only the transport changed (FastMCP stdio → in-process
`@tool` registry).

## DB location

Default `~/.day-receipt/receipt.db`. Override via `DAY_RECEIPT_DB`.
The standalone CLI and the in-process tools resolve the same default
path so they share data on the user's main device.

## Tools

- `receipt_add(category, text, date?, tags?)` — log one entry. Category
  must be one of `made` / `moved` / `learned` / `avoided`.
- `receipt_today()` — structured snapshot of today's receipt.
- `receipt_get(date)` — structured snapshot of any date.
- `receipt_print(date?, width?)` — render a date as 46-col ASCII slip.
- `receipt_week(days?, width?)` — render the last N days; empty days
  are skipped.
- `receipt_search(query, limit?)` — substring search over text + tags.
- `receipt_set_note(text, date?)` — set or clear the top-of-receipt
  note. Empty string clears.
- `receipt_delete(entry_id)` — delete one entry by id.
