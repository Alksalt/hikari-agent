# DuckDB analytics MCP

Read-only SQL analytics over Hikari's own SQLite stores. Use this when
the user asks trend questions ("my made count is up vs last month?",
"how many messages per day this week?", "what facts have I recalled
the most lately?").

## Server

Declared in `.mcp.json` as `duckdb`. Boots an in-memory DuckDB via
`uvx --from mcp-server-motherduck mcp-server-motherduck --db-path
:memory: --ephemeral-connections --max-rows 256 --query-timeout 15`.

DuckDB's `:memory:` mode is always writable (DuckDB limitation), but
we never touch user data — instead the agent attaches the user's
SQLite files at query time with `READ_ONLY`. `--ephemeral-connections`
keeps the SQLite files unlocked so the live bot can keep writing while
we read.

If `uvx` can't fetch the package on a fresh machine, run once to warm
the cache:
```bash
uvx --from mcp-server-motherduck mcp-server-motherduck --help
```

## Data sources

| Alias    | Path                                                  | Notes                          |
|----------|-------------------------------------------------------|--------------------------------|
| hikari   | `data/hikari.db`                                      | messages, facts, tasks, episodes, observations |
| receipts | `$DAY_RECEIPT_DB` or `~/.day-receipt/receipt.db`      | day_receipt entries (made / moved / learned / avoided) |

## ATTACH preamble

Every query begins with the same preamble. The agent inlines the
absolute paths.

```sql
INSTALL sqlite;
LOAD sqlite;
ATTACH '/Users/ol/agents/hikari-agent/data/hikari.db' AS hikari (TYPE sqlite, READ_ONLY);
ATTACH '/Users/ol/.day-receipt/receipt.db'           AS receipts (TYPE sqlite, READ_ONLY);
```

## Example queries

### Made-count by month (last 3 months)

```sql
SELECT date_trunc('month', CAST(receipt_date AS DATE)) AS month,
       COUNT(*) AS made_count
FROM receipts.entries
WHERE category = 'made'
  AND CAST(receipt_date AS DATE) >= CURRENT_DATE - INTERVAL 3 MONTH
GROUP BY 1
ORDER BY 1;
```

### Receipts trend by category (last 4 weeks)

```sql
SELECT date_trunc('week', CAST(receipt_date AS DATE)) AS week,
       category,
       COUNT(*) AS n
FROM receipts.entries
WHERE CAST(receipt_date AS DATE) >= CURRENT_DATE - INTERVAL 4 WEEK
GROUP BY 1, 2
ORDER BY 1, 2;
```

### Messages per day (last 30 days)

```sql
SELECT date_trunc('day', CAST(ts AS TIMESTAMP)) AS day,
       role,
       COUNT(*) AS n
FROM hikari.messages
WHERE CAST(ts AS TIMESTAMP) >= CURRENT_TIMESTAMP - INTERVAL 30 DAY
GROUP BY 1, 2
ORDER BY 1, 2;
```

### Most-recalled facts (last week)

```sql
SELECT subject,
       predicate,
       object,
       recall_hit_count,
       last_recalled_at
FROM hikari.facts
WHERE last_recalled_at IS NOT NULL
  AND CAST(last_recalled_at AS TIMESTAMP) >= CURRENT_TIMESTAMP - INTERVAL 7 DAY
  AND (status IS NULL OR status != 'invalid')
ORDER BY recall_hit_count DESC, last_recalled_at DESC
LIMIT 20;
```

## Caveats

- Read-only by contract. Never issue `INSERT` / `UPDATE` / `DELETE`
  against the attached SQLite stores — the `READ_ONLY` flag enforces
  this, but the agent should also pick queries that match the
  read-only spirit (analytics, not mutation).
- Result rows can contain attacker-shaped text (a fact's `object`, a
  message `content`). The PostToolUse wrap hook wraps duckdb output
  via `wrap_untrusted` before the model sees it — same defense as
  for `WebFetch` and the other external surfaces.
- `recall_hit_count` and `last_recalled_at` only get populated by the
  recall agent's writeback; older facts may have NULLs.
