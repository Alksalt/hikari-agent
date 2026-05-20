# Link Shelf

Personal save-for-later bucket. The user reads a lot, saves things,
never comes back. The shelf is write-mostly: capture now, resurface
later via search.

## Tools

| Tool | Use |
|---|---|
| `link_save` | Capture a URL with a `kind` (later / useful / source / inspiration) and tags. |
| `link_search` | Keyword search across title, snippet, tags, note. FTS5 + LIKE fallback. |
| `link_list` | Browse — newest first, optional kind/tag filter. |
| `link_update` | Change kind / tags / note on a saved link. |
| `link_delete` | Remove a link. |

## Kinds

- `later` — i'll read it later (default).
- `useful` — reference material i'll come back to.
- `source` — citation / where i learned X.
- `inspiration` — something to mull on.

## How Hikari uses it

When the user shares a URL, save it (default `later` if unclear).
Mid-conversation, if a topic comes up, call `link_search` to see if
the user already saved something on it — "i remember you sent me this".

## Files

- `__init__.py` — manifest, builds `lazy_tool` stubs.
- `db.py` — schema + CRUD (self-contained migration via `_ensure_schema`).
- `handlers.py` — actual implementations. Heavy deps (`httpx`) imported
  inside the handlers so the manifest stays cold.

## Schema

Two tables in the shared SQLite DB:

- `links` — id, url, title, snippet, kind, tags_json, note,
  added_at, last_recalled_at, recall_count, archived.
- `link_fts` — content-owning FTS5 over title + snippet + tags + note.

Unique index on `url` for active (non-archived) rows means saving the
same URL twice updates in place rather than duplicating.
