# Hikari Agent Ops / Production Runbook Review

Date: 2026-05-23

Scope: single-user local macOS Telegram companion, with optional Cloudflare-exposed external MCP.

This review is intentionally boring. The goal is not enterprise ceremony. The goal is a local companion that restarts, tells the owner when it is sick, has recoverable state, and can be re-created on a new Mac without folklore.

## Executive summary

Hikari already has the right bones for a durable solo-dev system:

- The main bot has a launchd installer (`scripts/install_launchd.sh`) that runs `uv run hikari-agent` as a user LaunchAgent with `RunAtLoad`, `KeepAlive`, log paths, and a short throttle interval.
- The Telegram bridge persists assistant messages only after successful send, which prevents phantom "Hikari said this" rows when Telegram delivery fails.
- `agents/runtime.py` cleanly splits user/proactive visible turns from stateless internal-control turns. Visible turns resume the live Claude Agent SDK session under `_RUN_LOCK`; internal control does not mutate the live SDK session.
- SQLite is the real memory and operations ledger (`data/hikari.db`), opened in WAL mode with a busy timeout. That is exactly the right primary store for one person on one machine.
- APScheduler is embedded and simple. For this size, it is appropriate, as long as job health is observable.
- External MCP is read-only, bound locally by default, authenticated by bearer and/or OAuth, and intended to sit behind Cloudflare Tunnel.
- A daily backup LaunchAgent exists and uses `sqlite3 .backup`, which is the right family of SQLite backup mechanism for a live WAL-mode database.

The current weak points are mostly operational, not architectural:

- Only the main bot and database backup have launchd installers. External MCP and `cloudflared` still need real service management.
- There is no single owner-visible `/status`, health-check script, startup health digest, or dead-man monitor.
- Backups are raw SQLite copies into iCloud; that is recoverable, but it also copies OAuth tokens and private memory to a synced location.
- Restore is not yet drilled. A backup that has never been restored is only a comforting file.
- Graphiti/Kuzu is a sidecar index, but the recovery contract is not explicit enough. SQLite should remain canonical; graph data should be treated as rebuildable until a durable outbox/backfill path exists.
- APScheduler jobs are in-memory and some capability-dependent jobs are added only if credentials are healthy at startup. A fixed credential later may require restart before those jobs exist.
- Telegram polling uses `drop_pending_updates=True`; this avoids backlog storms but means user messages sent during downtime may be dropped on restart.
- External MCP package entrypoints in `.mcp.json` / `config/tools.yaml` include floating `npx -y` / `@latest` style packages. That is a supply-chain and rollback risk for a long-lived companion.

The operational spine should be:

1. `launchd` owns the main bot, external MCP server, Cloudflare tunnel, backup job, and a tiny dead-man monitor.
2. SQLite is the canonical state. Back it up daily, scrub iCloud copies, and restore-test weekly.
3. Kuzu/Graphiti, embeddings, FTS, logs, and MCP generated files are rebuildable.
4. Hikari sends a startup health digest and has a cheap `/status`.
5. A non-LLM dead-man monitor pages the owner when the bot has been silent or unhealthy too long.
6. Every production action has a one-screen runbook: restart, inspect, restore, rotate, rollback.

## Current production topology

### Main process

Entrypoint: `hikari-agent = "agents.telegram_bridge:main"` in `pyproject.toml`.

The normal production process is:

```text
launchd user LaunchAgent
  -> uv run hikari-agent
    -> agents.telegram_bridge:main()
      -> python-telegram-bot Application.run_polling(...)
      -> agents.scheduler.build_scheduler(...)
      -> agents.runtime live Claude Agent SDK client
      -> SQLite memory/state in data/hikari.db
      -> optional Graphiti/Kuzu sidecar in data/hikari.kuzu
```

`scripts/install_launchd.sh` writes `~/Library/LaunchAgents/com.hikari.agent.plist` with:

- `ProgramArguments`: `uv run hikari-agent`
- `WorkingDirectory`: repo root
- `RunAtLoad`: true
- `KeepAlive`: restart on unsuccessful exit and watch network state
- `ProcessType`: `Interactive`
- stdout/stderr logs in `~/Library/Logs/hikari.log` and `~/Library/Logs/hikari.err`
- `ThrottleInterval`: 15 seconds

This matches the right macOS primitive. Apple describes launchd as the system facility for loading user agents at login, keeping always-on jobs running, and using `KeepAlive` for jobs that must be running continuously ([Apple launchd jobs](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html)).

### Telegram bridge

`agents/telegram_bridge.py` uses `python-telegram-bot` long polling. The app registers handlers for text, media, location, voice, documents, stickers, reactions, commands, approvals, and owner-only operations.

Important production behavior:

- `run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)` starts polling.
- Telegram messages are appended to SQLite inbound history.
- Assistant messages are persisted only after Telegram send succeeds.
- If Telegram send fails, the bridge logs the failure and avoids writing a fake assistant row.
- The bot drops Telegram updates waiting at restart. Telegram's Bot API stores incoming updates until consumed, but not longer than 24 hours; `drop_pending_updates` explicitly discards queued updates ([Telegram Bot API](https://core.telegram.org/bots/api#getting-updates), [deleteWebhook/drop_pending_updates](https://core.telegram.org/bots/api#deletewebhook)).

This is a reasonable solo-dev choice, but it must be visible in the silence runbook: if Hikari was down and then restarted, the user may need to resend what they said.

### Claude Agent SDK runtime

`agents/runtime.py` exposes three runtime entrypoints:

- `run_user_turn(user_text)`: visible user message, resumes the live SDK session via stored `session_id`, acquires `_RUN_LOCK`, updates stored SDK session ID from the SDK result, and does not persist the assistant reply itself.
- `run_visible_proactive(seed_prompt)`: visible proactive generation with the same live-session semantics; caller sends and persists after delivery.
- `run_internal_control(prompt)`: stateless control call with `resume=None`, no session write, no messages append, no `_RUN_LOCK`.

This is the core invariant: only sent visible text should become conversation history, and internal utility calls must not fork the companion's lived session. Anthropic's Agent SDK documentation treats sessions as the persisted conversation history that can be resumed across runs, while filesystem state is separate ([Claude Agent SDK sessions](https://code.claude.com/docs/en/agent-sdk/sessions)).

Production risk to track: multimodal/content-block paths use an ephemeral SDK path and have been flagged in prior reviews as a possible session fork. That is not a day-one ops blocker, but it belongs in P1.

### APScheduler

`agents/scheduler.py` creates an `AsyncIOScheduler` in `Europe/Oslo`. Jobs include:

- heartbeat every 30 minutes
- calendar heartbeat every 5 minutes, only if calendar credentials are healthy at scheduler construction
- reengagement every 15 minutes
- session consolidation every 15 minutes
- reminders every 60 seconds
- Apple reminder sync every 300 seconds on Darwin
- Google Calendar reminder sync every 300 seconds, only if credentials are healthy at scheduler construction
- daily reflection at 09:00
- morning brief at 06:00
- memory prune monthly
- daily check-in polling
- evening diary
- drift canary
- future letter
- decision resolver
- weekly consolidation
- wiki new-file scan

APScheduler is appropriate here because jobs are lightweight and local. The important operational detail is that the default in-memory job store does not survive process restart. APScheduler's own docs call out `max_instances`, `misfire_grace_time`, coalescing, job listing, and event listeners as the tools for this exact class of operational visibility ([APScheduler user guide](https://apscheduler.readthedocs.io/en/3.x/userguide.html)).

### External MCP and Cloudflare edge

External MCP is configured under `mcp_external` in `config/engagement.yaml`:

- enabled: true
- bind host: `127.0.0.1`
- bind port: `8765`
- public base URL: `https://hikari.alksalt.com`
- behind TLS proxy: true
- OAuth access token TTL: 1 hour
- OAuth refresh token TTL: 30 days
- passphrase failure limit: 5 per 300 seconds

`mcp_external.launch` runs a Starlette/FastMCP Streamable HTTP server. It refuses to start unless either `HIKARI_MCP_SECRET` or `HIKARI_OAUTH_OWNER_PASSPHRASE` is set. It supports:

- static bearer auth through `HIKARI_MCP_SECRET`
- OAuth 2.1 style auth with PKCE, DCR, opaque access/refresh tokens in SQLite
- read-only tools only: recall, lexicon, observations, open loops, wiki search
- audit rows for external tool calls
- log output to `data/logs/mcp_external.log`

Cloudflare Tunnel is the intended public ingress. Cloudflare's docs describe Tunnel as an outbound-only connector from the local service to Cloudflare, avoiding public inbound ports; their macOS service docs support installing `cloudflared` as a launch agent or daemon ([Cloudflare Tunnel overview](https://developers.cloudflare.com/tunnel/), [cloudflared macOS service](https://developers.cloudflare.com/tunnel/advanced/local-management/as-a-service/macos/)).

Current gap: `scripts/install_cloudflared.md` documents manual MCP and tunnel startup, but does not yet install `com.hikari.mcp` or `com.hikari.tunnel`.

### Durable storage

Canonical state is SQLite:

```text
data/hikari.db
data/hikari.db-wal
data/hikari.db-shm
```

`storage/db.py` owns schema creation and data access. It stores session IDs, messages, facts, episodes, runtime state, background tasks, approvals, audit logs, reminders, OAuth clients/codes/tokens, feedback, diaries metadata, drift probes, decisions, and more.

Graphiti/Kuzu is sidecar state:

```text
data/hikari.kuzu
```

`storage/graph.py` initializes this lazily if `OPENROUTER_API_KEY` is available. Graph writes are best-effort and Graph search fails closed. Treat this as rebuildable until there is a durable outbox from SQLite and a tested graph replay.

### Backup process

`scripts/install_backup.sh` installs `com.hikari.backup.plist`, a daily 03:00 LaunchAgent that runs `scripts/backup.sh`.

`scripts/backup.sh` currently:

- backs up `data/hikari.db`
- writes to the Obsidian/iCloud wiki path under `projects/hikari-agent/backups`
- uses `sqlite3 "$SRC" ".backup '$DEST'"`
- retains the last 14 backups

SQLite's online backup API creates a consistent snapshot of a live database; `VACUUM INTO` is also an official live-database backup option and purges deleted content in the output copy ([SQLite Online Backup API](https://www.sqlite.org/backup.html), [SQLite VACUUM INTO](https://sqlite.org/lang_vacuum.html#vacuuminto)).

Current gap: the iCloud copy is raw and likely includes OAuth tokens, private messages, facts, and audit rows.

## State inventory: durable, rebuildable, ephemeral

### Durable and high value

These must survive machine failure or accidental deletion:

| State | Location | Why it matters | Backup stance |
| --- | --- | --- | --- |
| SQLite canonical DB | `data/hikari.db` | Messages, facts, tasks, runtime state, reminders, OAuth tokens, approvals, audit log | Daily live backup, restore drill |
| User uploads | `data/user_photos/`, likely `data/user_documents/`, `data/user_voice/` when present | Source material user gave Hikari | Back up if the user expects recall/media continuity |
| Diaries / local artifacts | `data/diary/`, future letters if file-backed | Personal record and generated continuity | Back up with DB |
| Secrets | `.env`, Keychain entries, `secrets/google_oauth_client.json`, Cloudflare tunnel credentials | Required to boot and connect | Prefer Keychain/manual rotation docs; do not sync raw secrets casually |
| Repo and config | Git checkout, `config/*.yaml`, `CLAUDE.md`, `AGENTS.md`, `.mcp.json`, `pyproject.toml`, `uv.lock` | Defines behavior and dependency graph | Git is the backup; record commit in backup manifest |
| Cloudflare tunnel identity | `~/.cloudflared/config.yml`, tunnel credentials JSON, Cloudflare dashboard config | Required for public MCP URL | Back up or document recreate procedure |
| Obsidian wiki | iCloud vault path | User knowledge base and current backup destination | Already iCloud-backed, but still needs restore expectations |

### Durable but rebuildable

These can be recreated if SQLite and config survive:

| State | Location | Rebuild method |
| --- | --- | --- |
| Graphiti/Kuzu graph | `data/hikari.kuzu` | Recreate from SQLite facts/episodes after outbox/backfill is reliable |
| sqlite-vec embeddings | `vec_facts`, `vec_episodes` in SQLite | `scripts/backfill_embeddings.py` |
| FTS tables | SQLite FTS virtual tables/triggers | Rebuild from source tables if needed |
| `.mcp.json` | repo root | `uv run python scripts/regen_mcp_json.py` |
| Logs | `data/logs/*.log`, `~/Library/Logs/hikari*.log` | Not required for continuity |
| External OAuth access tokens | SQLite `oauth_tokens` | Re-authorize clients with passphrase |
| External MCP dynamic clients | SQLite `oauth_clients` | Re-register clients |

### Ephemeral

These do not need backup:

- In-process Claude SDK client object
- In-memory APScheduler state
- In-memory OAuth passphrase rate limiter
- Telegram typing indicators
- Running background dispatch tasks
- Current Cloudflare tunnel TCP connections
- `data/hikari.db-wal` and `data/hikari.db-shm` when restoring from a clean `.backup` snapshot while services are stopped
- OAuth authorization codes
- Any generated text that was not successfully sent and persisted

## Failure-mode table

| Failure | User symptom | Likely cause | First check | Recovery | Page owner? |
| --- | --- | --- | --- | --- | --- |
| Main bot process down | No replies; no reminders; no proactive messages | crash, bad env, dependency failure, laptop asleep | `launchctl print gui/$(id -u)/com.hikari.agent`; `tail -100 ~/Library/Logs/hikari.err` | `launchctl kickstart -k gui/$(id -u)/com.hikari.agent`; if it loops, stop and run `uv run hikari-agent` foreground | Yes if down > 10-15 min |
| LaunchAgent crash loop | Repeated restarts; high log churn | startup exception, auth precheck, DB lock, bad dependency | `launchctl print ...` last exit status; `~/Library/Logs/hikari.err`; `data/logs/hikari.log` | Fix root cause, then `launchctl kickstart -k ...`; rollback if package/config regression | Yes |
| Telegram token invalid | Process running but cannot receive/send | BotFather rotation, wrong token, Telegram API failure | `getMe` via Bot API; app logs for Unauthorized/NetworkError | Update token in Keychain/env; restart; send owner ping | Yes |
| Telegram webhook conflict | Polling receives nothing | Webhook still configured | `getWebhookInfo`; Bot API says `getUpdates` will not work with webhook set | `deleteWebhook` with desired `drop_pending_updates` value; restart | Yes |
| Telegram pending updates dropped | User says "I messaged while she was down" but Hikari never saw it | `drop_pending_updates=True` on polling startup | bridge startup logs; Telegram pending count before restart if available | Ask user to resend; this is intentional backlog control | No, but explain |
| Claude SDK auth failure | Hikari sends SDK-error-looking fallback or no response | expired `CLAUDE_CODE_OAUTH_TOKEN`, stale Claude login, SDK process error | `data/logs/hikari.log` for ProcessError/auth; foreground SDK probe | Re-run `claude setup-token`; restart; if session is stale, clear stored SDK session ID and let runtime start fresh | Yes |
| Claude SDK session stale/forked | Replies lose context or resume wrong thread | stale persisted `session_id`, content-block path fork | Inspect `session` table and recent logs | Clear or rotate SDK session deliberately; keep SQLite memory intact | Yes if persistent |
| Scheduler not running | No reminders/proactive/calendar jobs | scheduler failed in post-init, startup exception | app startup logs; future `/status` job list | Restart bot; add scheduler event listener and `/status` in P0/P1 | Yes |
| Calendar jobs missing | Google/Apple reminders do not sync, but bot works | credential probe unhealthy at scheduler construction | startup health logs; `runtime_state.calendar_heartbeat_healthy`; Google auth status | Reauth, then restart so jobs are registered | Yes if user depends on reminders |
| Duplicate proactive messages | Multiple pings close together | independent scheduler jobs, no global proactive reservation | `proactive_events`, messages table timestamps | Manually silence; implement global reservation/idempotency | Yes if noisy |
| SQLite locked | Delayed/failed replies, OperationalError | long write, backup, rogue script, stuck process | logs; `lsof data/hikari.db`; `sqlite3 data/hikari.db "pragma quick_check"` | Stop rogue process; restart; increase visibility before changing timeout | Yes |
| SQLite corruption | startup fail, quick_check fail | disk issue, bad copy/restore, power loss | `sqlite3 data/hikari.db "pragma quick_check"` | Stop services; restore latest verified backup; preserve corrupt file for analysis | Yes |
| Disk full | Everything degrades; DB/log writes fail | logs, uploads, backups, WAL growth | `df -h`; `du -sh data ~/Library/Logs/hikari*` | Stop nonessential services; prune logs/backups/uploads; restart | Yes |
| Backup failed/stale | No recent backup file | LaunchAgent unloaded, iCloud path unavailable, sqlite3 error | `~/Library/Logs/hikari-backup.err`; latest backup mtime | Run `scripts/backup.sh` manually; fix path; reinstall backup LaunchAgent | Yes if > 36h |
| Raw backup leaked to iCloud | Sensitive data synced | current backup copies full DB | backup destination | Move to scrubbed iCloud backup plus local raw encrypted backup | Yes, security event if exposed |
| Graphiti/Kuzu unavailable | Graph recall weaker; logs show graph degraded | missing `OPENROUTER_API_KEY`, Kuzu lock, corrupt graph path | `data/logs/hikari.log`; `du -sh data/hikari.kuzu` | Treat graph as degraded; keep SQLite recall; rebuild graph later | No unless user notices |
| External MCP local 401 | Claude Desktop/iPhone connector cannot call tools | wrong bearer/OAuth token, missing env | `curl` local `/mcp` with bearer; MCP logs | Rotate `HIKARI_MCP_SECRET` or reauthorize OAuth | Yes if external MCP is used |
| External MCP process down | Cloudflare host reachable but MCP 502/connection refused | no LaunchAgent, crash, port conflict | `lsof -i :8765`; `data/logs/mcp_external.log` | Start/restart `uv run python -m mcp_external.launch`; add LaunchAgent | Yes if used |
| Cloudflare tunnel down | Public MCP URL dead; local MCP works | cloudflared stopped, tunnel creds invalid, network | Cloudflare dashboard; `cloudflared tunnel info`; tunnel logs | Restart cloudflared; re-login/recreate tunnel if creds invalid | Yes if used |
| OAuth DCR spam | `oauth_clients` / audit grows | open DCR exposed to internet | row counts in SQLite; MCP logs | Add caps/rate limits; prune clients; consider pre-registration | Yes if growth/abuse |
| Google/Notion/GitHub creds expired | Calendar/Drive/Notion/GitHub tools fail | revoked refresh token, testing-mode expiry, PAT rotation | `uv run python -m scripts.auth <provider> status` | Run provider grant/paste flow; restart if startup-gated | Yes if core workflow |
| Dependency break | Startup fails after update | floating MCP packages or new package release | recent shell history, `uv.lock`, `.mcp.json` package specs | Roll back git/lockfile/package pin; `uv sync --frozen` | Yes |
| Mac slept/powered off | No replies until wake | local-first machine unavailable | macOS power logs; last heartbeat file | Wake machine; consider energy settings/UPS | Yes only if extended |

## Health-check design

Add one boring command:

```bash
uv run python scripts/health_check.py
```

It should emit both human text and JSON:

```bash
uv run python scripts/health_check.py --json
```

Exit codes:

- `0`: green
- `1`: yellow/degraded, core bot can still reply
- `2`: red, core bot may be dead or data at risk

### Checks

Core red checks:

- Main LaunchAgent loaded and process alive:
  - `launchctl print gui/$(id -u)/com.hikari.agent`
  - report last exit status and PID
- SQLite health:
  - DB exists
  - `pragma quick_check`
  - WAL/shm size
  - newest inbound message timestamp
  - newest outbound assistant message timestamp
  - newest runtime heartbeat/status row once implemented
- Disk:
  - free space above 5 GB
  - `data/` below warning threshold or growth trend known
- Telegram:
  - token present
  - `getMe` succeeds
  - `getWebhookInfo.url` empty for polling mode
- Backup:
  - latest backup exists
  - latest backup younger than 36 hours
  - latest backup `pragma quick_check` succeeds
- Claude SDK:
  - `CLAUDE_CODE_OAUTH_TOKEN` present or documented auth source present
  - last SDK error age/count from logs or ops events
  - optional manual tiny probe, not on every scheduler tick

Core yellow checks:

- Scheduler:
  - scheduler started
  - expected job IDs registered
  - next run times visible
  - last job exception from APScheduler listener
- Calendar credential health:
  - Google startup probe result
  - calendar/reminder jobs present only when creds are healthy
- Graph:
  - `OPENROUTER_API_KEY` present if graph enabled
  - Kuzu path exists
  - last graph error timestamp
  - graph is marked "rebuildable" if down
- External MCP:
  - local server on `127.0.0.1:8765` if enabled
  - `/.well-known/oauth-protected-resource` returns expected metadata
  - unauthenticated `/mcp` returns 401 with resource metadata
  - bearer/OAuth test call succeeds if test credential is configured
- Cloudflare:
  - `cloudflared` service alive
  - public base URL resolves
  - public metadata endpoint returns expected issuer/resource
- Secrets:
  - production has `HIKARI_REQUIRE_KEYCHAIN=1`
  - `.env` contains only expected low-risk config, not every long-lived token

### Owner-visible status

Add `/status` in Telegram with a short green/yellow/red summary:

```text
status: yellow
bot: running, pid 12345, restarted 2h ago
telegram: ok
claude: ok, last sdk error 0
sqlite: ok, 4.3 MB, quick_check ok
backup: ok, latest 2026-05-23 03:00
scheduler: 18 jobs, 0 failing
graph: degraded, OPENROUTER_API_KEY missing
mcp: ok local, tunnel down
disk: 42 GB free
```

Do not involve Claude in computing `/status`. It should be deterministic, fast, and safe to run when the LLM is broken.

### Startup health digest

After `telegram_bridge.post_init` starts scheduler and probes Google, send the owner a compact startup digest when any check is yellow/red:

```text
booted, but degraded:
- google calendar auth invalid_grant; calendar jobs not scheduled
- backup is 49h old
- external mcp enabled but :8765 is not reachable
```

If everything is green, either stay quiet or send a once-per-day "booted healthy" note.

### Dead-man switch

Add a tiny non-LLM heartbeat file:

```text
data/health/last_success.json
```

Update it after:

- successful Telegram polling startup
- successful send to owner
- each scheduler tick
- each backup success

Then install a separate LaunchAgent, for example `com.hikari.deadman`, every 5 minutes:

- reads the heartbeat file
- checks `launchctl print` for `com.hikari.agent`
- checks latest backup age
- if stale > 15 minutes while Mac is awake, sends owner alert through a minimal Telegram Bot API call
- if Telegram is unreachable, uses a local macOS notification or fallback email command

The dead-man must not import the full Hikari runtime and must not call Claude. It is a smoke alarm, not another agent.

## Restart/supervision plan

### Services to supervise

Minimum production service set:

| Label | Purpose | Required? | Supervisor |
| --- | --- | --- | --- |
| `com.hikari.agent` | Main Telegram companion | Yes | launchd user LaunchAgent |
| `com.hikari.backup` | Daily SQLite backup | Yes | launchd user LaunchAgent |
| `com.hikari.deadman` | Independent stale-health alert | Yes | launchd user LaunchAgent |
| `com.hikari.mcp` | External read-only MCP server | If external MCP enabled | launchd user LaunchAgent |
| `com.hikari.tunnel` or `com.cloudflare.cloudflared` | Cloudflare Tunnel | If external MCP enabled | launchd user LaunchAgent or Cloudflare-installed service |

For Hikari, user LaunchAgents are preferable to root LaunchDaemons because the app depends on user-scoped Keychain, Apple Automation permissions, iCloud paths, and the user's repo checkout. Cloudflare can use either Cloudflare's own macOS service install or a local `com.hikari.tunnel` user agent; choose one, document it, and do not run two tunnel supervisors at once.

### Daily commands

Inspect:

```bash
launchctl print gui/$(id -u)/com.hikari.agent
tail -100 ~/Library/Logs/hikari.err
tail -100 data/logs/hikari.log
```

Restart:

```bash
launchctl kickstart -k gui/$(id -u)/com.hikari.agent
```

Stop:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.hikari.agent.plist
```

Start:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hikari.agent.plist
launchctl enable gui/$(id -u)/com.hikari.agent
launchctl kickstart -k gui/$(id -u)/com.hikari.agent
```

Foreground debug:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.hikari.agent.plist
uv run hikari-agent
```

### LaunchAgent requirements

Keep these properties for every long-lived service:

- `Label`
- `ProgramArguments`
- `WorkingDirectory`
- `RunAtLoad`
- `KeepAlive` with restart on unsuccessful exit
- `StandardOutPath`
- `StandardErrorPath`
- `ThrottleInterval`
- explicit `EnvironmentVariables` for production-only flags:
  - `HIKARI_REQUIRE_KEYCHAIN=1`
  - minimal `PATH`

Do not daemonize inside the process. Apple's launchd guidance explicitly warns managed processes not to fork into the background because launchd will think the process died ([Apple launchd jobs](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html)).

### External MCP LaunchAgent

Target service:

```text
Label: com.hikari.mcp
ProgramArguments:
  /path/to/uv
  run
  python
  -m
  mcp_external.launch
WorkingDirectory: /Users/ol/agents/hikari-agent
RunAtLoad: true
KeepAlive: SuccessfulExit=false
Logs:
  ~/Library/Logs/hikari-mcp.log
  ~/Library/Logs/hikari-mcp.err
```

The server already writes rotating logs to `data/logs/mcp_external.log`. The launchd logs catch import/startup failures before app logging is configured.

### Cloudflare tunnel service

Prefer Cloudflare's native macOS service if it is stable on this machine:

```bash
cloudflared service install
launchctl start com.cloudflare.cloudflared
```

Cloudflare documents that a non-sudo macOS install creates a launch agent using `~/.cloudflared/`, while `sudo cloudflared service install` creates a boot service using `/etc/cloudflared` ([cloudflared macOS service](https://developers.cloudflare.com/tunnel/advanced/local-management/as-a-service/macos/)).

If using a Hikari-owned user LaunchAgent instead, target:

```bash
cloudflared tunnel run hikari-mcp
```

and log to:

```text
~/Library/Logs/hikari-tunnel.log
~/Library/Logs/hikari-tunnel.err
```

Only one supervisor should own `cloudflared` for this tunnel.

## Backup and restore plan

### Backup tiers

P0 backup model:

1. Local raw backup, encrypted or at least mode `0600`
   - destination: `~/Library/Application Support/Hikari/backups/raw/`
   - contains full SQLite DB, including OAuth tokens
   - retention: 30 daily, 8 weekly
2. iCloud scrubbed backup
   - destination can remain the Obsidian wiki backup folder
   - remove or null sensitive tables/columns before the final copy:
     - `oauth_tokens`
     - `oauth_codes`
     - any raw pending approval arguments that may contain secrets
     - high-risk audit arg payloads if they may contain credentials
   - use `VACUUM INTO` for a compact scrubbed copy, because SQLite documents that it purges deleted content from the output copy ([SQLite VACUUM INTO](https://sqlite.org/lang_vacuum.html#vacuuminto))
3. Backup manifest
   - write a small adjacent JSON file:
     - timestamp
     - repo commit SHA
     - `uv.lock` hash
     - `pyproject.toml` hash
     - DB size
     - quick_check result
     - row counts for key tables
     - machine hostname

The current `scripts/backup.sh` is directionally right because it uses SQLite backup instead of copying WAL-mode files directly. It should be extended, not replaced.

### What to back up

Always:

- `data/hikari.db` via SQLite backup API / `.backup`
- `data/diary/`
- `data/user_photos/`
- any `data/user_documents/` or voice attachment roots if present
- backup manifest

Usually via Git rather than backup bundle:

- `AGENTS.md`
- `CLAUDE.md`
- `config/*.yaml`
- `.mcp.json`
- `pyproject.toml`
- `uv.lock`
- `scripts/*.sh`, `scripts/*.py`

Optional:

- `data/hikari.kuzu`, but only with service stopped or when Kuzu supports a known-safe backup. Until then, treat graph as rebuildable.
- `data/logs/` for incident analysis, with short retention.

Never casually sync raw:

- `.env`
- Keychain export
- Telegram bot token
- Claude OAuth token
- OpenRouter key
- Google/Notion/GitHub tokens
- Cloudflare tunnel credentials

For secrets, prefer a password-manager note or an operator checklist that says how to rotate/recreate them.

### Manual backup

```bash
uv run python scripts/validate_tool_registry.py
scripts/backup.sh
sqlite3 "/path/to/latest/backup.db" "pragma quick_check;"
```

If implementing the scrubbed split:

```bash
sqlite3 data/hikari.db ".backup '/tmp/hikari-raw.db'"
cp /tmp/hikari-raw.db "/secure/local/raw/hikari-$(date +%Y%m%d).db"
sqlite3 /tmp/hikari-raw.db "delete from oauth_tokens; delete from oauth_codes;"
sqlite3 /tmp/hikari-raw.db "vacuum into '/tmp/hikari-scrubbed.db';"
sqlite3 /tmp/hikari-scrubbed.db "pragma quick_check;"
cp /tmp/hikari-scrubbed.db "$ICLOUD_DEST"
```

The exact script should avoid shell quoting footguns, but the recovery contract is the important part.

### Restore from backup

1. Stop services:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.hikari.agent.plist
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.hikari.mcp.plist
```

2. Preserve current broken state:

```bash
mkdir -p data/restore-hold
cp data/hikari.db data/restore-hold/hikari.db.before-restore
cp data/hikari.db-wal data/restore-hold/hikari.db-wal.before-restore 2>/dev/null || true
cp data/hikari.db-shm data/restore-hold/hikari.db-shm.before-restore 2>/dev/null || true
```

3. Restore the snapshot:

```bash
cp /path/to/verified-backup.db data/hikari.db
rm -f data/hikari.db-wal data/hikari.db-shm
chmod 600 data/hikari.db
sqlite3 data/hikari.db "pragma quick_check;"
```

4. Start:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hikari.agent.plist
launchctl kickstart -k gui/$(id -u)/com.hikari.agent
```

5. Verify:

- send a Telegram ping
- run health check
- confirm latest message/facts/reminders are present
- if graph is missing, let Hikari run without graph until rebuild

### Restore drill

Weekly:

- copy latest backup to `/tmp/hikari-restore-test.db`
- run `pragma quick_check`
- query row counts for `messages`, `facts`, `tasks`, `reminders`, `runtime_state`
- optionally run Hikari with `HIKARI_DB_PATH=/tmp/hikari-restore-test.db` in foreground without sending Telegram messages

Monthly:

- do a real restore into a temporary clone or second macOS user
- prove startup, `/status`, and a Telegram send

## New-machine restore checklist

### Prepare the Mac

- Install Xcode Command Line Tools.
- Install Homebrew if needed.
- Install `uv`.
- Install `cloudflared`.
- Ensure `sqlite3` is available.
- Sign in to iCloud if the wiki/backup path is iCloud-backed.
- Configure macOS Energy settings so the machine does not sleep through expected service hours, or accept that local-first means sleep equals offline.

### Restore repo and dependencies

```bash
git clone <repo-url> /Users/ol/agents/hikari-agent
cd /Users/ol/agents/hikari-agent
git checkout <known-good-commit>
uv sync --frozen
uv run python scripts/validate_tool_registry.py
```

If `uv sync --frozen` fails, do not upgrade packages as the first move. Restore the known lockfile or roll back to a commit that matches the backup manifest.

### Restore data

```bash
mkdir -p data
cp /path/to/verified-hikari.db data/hikari.db
chmod 600 data/hikari.db
sqlite3 data/hikari.db "pragma quick_check;"
```

Restore attachment/artifact folders if they matter:

```bash
rsync -a /backup/data/user_photos/ data/user_photos/
rsync -a /backup/data/diary/ data/diary/
```

Do not restore old `hikari.db-wal` or `hikari.db-shm` next to a clean backup snapshot.

### Restore secrets and accounts

Main:

- Run `claude setup-token` and set `CLAUDE_CODE_OAUTH_TOKEN` through the approved local path.
- Set `TELEGRAM_BOT_TOKEN`.
- Set `OWNER_TELEGRAM_ID`.
- Set `OPENROUTER_API_KEY` if Graphiti/media features are enabled.
- Set `HIKARI_REQUIRE_KEYCHAIN=1` for production LaunchAgents.

Provider auth:

```bash
uv run python -m scripts.auth google grant
uv run python -m scripts.auth google status
uv run python -m scripts.auth notion grant
uv run python -m scripts.auth notion status
uv run python -m scripts.auth github paste
uv run python -m scripts.auth github status
```

Note: Notion's local callback path currently uses the external MCP port family. If `127.0.0.1:8765` is occupied, stop external MCP during Notion reauth or change the callback port.

Cloudflare:

- Restore `~/.cloudflared/config.yml` and tunnel credentials JSON, or recreate the tunnel from Cloudflare.
- Confirm public hostname maps to local `http://127.0.0.1:8765`.
- Confirm Cloudflare account access and tunnel health in the dashboard.

macOS permissions:

- Run Hikari foreground once.
- Trigger Apple Notes/Reminders/Calendar paths if used so macOS prompts for Automation permissions.
- Grant permissions while the same user account is logged in.

### Install services

```bash
scripts/install_launchd.sh
scripts/install_backup.sh
```

Then install:

- `com.hikari.deadman`
- `com.hikari.mcp` if external MCP enabled
- Cloudflare tunnel service via `cloudflared service install` or a Hikari-owned tunnel LaunchAgent

### Verify

```bash
uv run pytest -q
uv run python scripts/validate_tool_registry.py
launchctl print gui/$(id -u)/com.hikari.agent
sqlite3 data/hikari.db "pragma quick_check;"
```

Then:

- send Telegram "ping"
- run `/status`
- create a test reminder
- run backup once
- verify Cloudflare MCP metadata endpoint if external MCP is enabled

## Cloudflare/external MCP runbook

### Expected healthy state

Local:

```bash
lsof -iTCP:8765 -sTCP:LISTEN
curl -i http://127.0.0.1:8765/.well-known/oauth-protected-resource
curl -i http://127.0.0.1:8765/mcp
```

Expected unauthenticated `/mcp` result:

- HTTP 401
- `WWW-Authenticate` header includes `resource_metadata=.../.well-known/oauth-protected-resource`

Public:

```bash
curl -i https://hikari.alksalt.com/.well-known/oauth-protected-resource
```

Expected:

- resource is `https://hikari.alksalt.com`
- authorization server points to the same base URL
- scopes include `mcp`

Bearer smoke:

```bash
curl -i \
  -H "Authorization: Bearer $HIKARI_MCP_SECRET" \
  https://hikari.alksalt.com/mcp
```

OAuth smoke:

- connector can discover protected resource metadata
- authorization page loads
- owner passphrase works
- token exchange succeeds
- access token works for `/mcp`

The MCP authorization spec now expects protected resource metadata, `WWW-Authenticate` discovery, PKCE, short-lived tokens, refresh-token rotation, and explicit resource/audience handling ([MCP Authorization 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)). The local implementation has most of this shape, but prior modernity review flagged the latest `resource` parameter/audience validation as a gap to close.

### Start order

1. Start external MCP:

```bash
uv run python -m mcp_external.launch
```

or:

```bash
launchctl kickstart -k gui/$(id -u)/com.hikari.mcp
```

2. Start Cloudflare tunnel:

```bash
cloudflared tunnel run hikari-mcp
```

or the installed service.

3. Test local metadata, public metadata, and one authenticated call.

### If public URL is down

1. Check local MCP:

```bash
curl -i http://127.0.0.1:8765/.well-known/oauth-protected-resource
```

2. If local is down, inspect:

```bash
tail -100 ~/Library/Logs/hikari-mcp.err
tail -100 data/logs/mcp_external.log
```

3. If local is up but public is down:

```bash
cloudflared tunnel info hikari-mcp
tail -100 ~/Library/Logs/hikari-tunnel.err
```

4. Check Cloudflare dashboard:

- tunnel connected
- public hostname route points to `http://127.0.0.1:8765`
- no Access policy unexpectedly blocking the connector client

### If connector gets 401

- Confirm whether it uses bearer or OAuth.
- For bearer, compare the secret used by the client with the secret visible to `com.hikari.mcp`.
- For OAuth, check:
  - `oauth_clients` row exists
  - access token not expired
  - refresh token not revoked
  - passphrase attempts not rate-limited
  - public base URL matches exactly
- Restart MCP only after confirming env and DB are correct.

### If DCR/audit tables grow

Current DCR is intentionally open. For a solo public endpoint, add:

- body size caps
- redirect URI count caps
- client name length caps
- per-IP DCR rate limit
- stale client pruning
- `/status` row counts for `oauth_clients`, `oauth_tokens`, `oauth_audit_log`

If abuse is visible before code changes, rotate the public URL or disable OAuth temporarily and use bearer-only with a new secret.

## Telegram outage/silence runbook

Use this when Hikari is unexpectedly silent.

### 1. Check intentional silence

```bash
sqlite3 data/hikari.db "select key, value from runtime_state where key like '%silence%';"
```

Also check whether the user recently sent `/silence`.

### 2. Check process

```bash
launchctl print gui/$(id -u)/com.hikari.agent
tail -100 ~/Library/Logs/hikari.err
tail -100 data/logs/hikari.log
```

If unloaded:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hikari.agent.plist
launchctl kickstart -k gui/$(id -u)/com.hikari.agent
```

If loaded but wedged:

```bash
launchctl kickstart -k gui/$(id -u)/com.hikari.agent
```

### 3. Check Telegram API

Without putting the token in logs:

```bash
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe"
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo"
```

Healthy polling mode:

- `getMe.ok` is true
- `getWebhookInfo.result.url` is empty

Telegram says `getUpdates` and webhooks are mutually exclusive; if a webhook is set, polling will not work ([Telegram Bot API getUpdates](https://core.telegram.org/bots/api#getupdates)).

### 4. Check DB/disk

```bash
df -h
du -sh data ~/Library/Logs/hikari* 2>/dev/null
sqlite3 data/hikari.db "pragma quick_check;"
```

If disk is full, free space before restarting repeatedly.

### 5. Check Claude SDK

Signs:

- logs show `ProcessError`, auth error, SDK timeout
- Hikari sends "SDK error" fallback text
- no response after inbound user row is stored

Recovery:

- re-run `claude setup-token`
- restart Hikari
- if only session is stale, clear the stored Claude SDK session ID and let Hikari begin a fresh live session while preserving SQLite memory

### 6. Foreground boot

If launchd logs are not enough:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.hikari.agent.plist
uv run hikari-agent
```

Fix what is printed, then reinstall/kickstart launchd.

### 7. Tell the user what happened

If `drop_pending_updates=True` was in effect during restart, say plainly:

```text
I was down and restarted in drop-pending mode, so Telegram may have discarded messages you sent while I was offline. Please resend the important bit.
```

This is not a bug. It is the chosen backlog-safety tradeoff.

## Credential rotation runbook

### Telegram bot token

1. Rotate/regenerate in BotFather.
2. Update Keychain/env source.
3. Restart `com.hikari.agent`.
4. Verify:

```bash
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe"
```

5. Send owner test message.

### Claude Code OAuth token

1. Run:

```bash
claude setup-token
```

2. Update production secret source.
3. Restart Hikari.
4. Send a short Telegram ping.
5. Watch logs for SDK auth/process errors.

### OpenRouter

1. Rotate key in OpenRouter.
2. Update secret source.
3. Restart Hikari if graph/media features need it immediately.
4. Verify Graphiti/Kuzu no longer logs missing-key degradation.

### Google

```bash
uv run python -m scripts.auth google revoke
uv run python -m scripts.auth google grant
uv run python -m scripts.auth google status
launchctl kickstart -k gui/$(id -u)/com.hikari.agent
```

If calendar jobs were skipped at startup because creds were unhealthy, restart is required so `agents.scheduler` registers them.

### Notion

```bash
uv run python -m scripts.auth notion revoke
uv run python -m scripts.auth notion grant
uv run python -m scripts.auth notion status
```

If the callback port conflicts with external MCP, stop `com.hikari.mcp` during the grant flow.

### GitHub

1. Create a new PAT with the minimal scopes required.
2. Store:

```bash
uv run python -m scripts.auth github paste
uv run python -m scripts.auth github status
```

3. Restart if the token was already injected into a running SDK environment.

### External MCP bearer and OAuth

Bearer:

1. Generate new `HIKARI_MCP_SECRET`.
2. Update service env.
3. Restart `com.hikari.mcp`.
4. Update clients using bearer.

OAuth owner passphrase:

1. Rotate `HIKARI_OAUTH_OWNER_PASSPHRASE`.
2. Restart `com.hikari.mcp`.
3. Existing refresh tokens remain valid unless explicitly revoked. To force reauth, clear/revoke `oauth_tokens`.

Cookie secret:

1. Rotate `HIKARI_OAUTH_COOKIE_SECRET` only if needed.
2. Restart MCP.
3. In-flight authorization pages will fail and must be restarted.

### Cloudflare tunnel credentials

1. Recreate or rotate tunnel credentials in Cloudflare.
2. Update `~/.cloudflared/config.yml` and credentials JSON.
3. Restart the tunnel service.
4. Confirm public hostname and metadata endpoint.

## Logging and alerting plan

### Existing logs

Main bot:

- `~/Library/Logs/hikari.log`
- `~/Library/Logs/hikari.err`
- `data/logs/hikari.log` from `telegram_bridge` rotating file handler

Backup:

- `~/Library/Logs/hikari-backup.log`
- `~/Library/Logs/hikari-backup.err`

External MCP:

- `data/logs/mcp_external.log`
- future `~/Library/Logs/hikari-mcp.log`
- future `~/Library/Logs/hikari-mcp.err`

Cloudflare:

- Cloudflare-installed macOS service logs, or future `~/Library/Logs/hikari-tunnel.*`

SQLite audit:

- `audit_log`
- `oauth_audit`
- approvals/background task tables

Redaction:

- `agents/log_scrub.py` installs a root logging filter and should remain on every executable entrypoint.
- Any future health/alert scripts must reuse redaction or avoid printing secrets.

### Structured ops events

Add a simple SQLite table or JSONL file. SQLite is better because `/status` can query it.

Suggested table:

```sql
ops_events(
  id integer primary key,
  ts text not null,
  severity text not null,
  component text not null,
  event text not null,
  summary text not null,
  details_json text
)
```

Events to emit:

- `startup_begin`
- `startup_healthy`
- `startup_degraded`
- `shutdown_signal`
- `telegram_polling_started`
- `telegram_send_failed`
- `sdk_turn_timeout`
- `sdk_process_error`
- `sdk_session_reset`
- `scheduler_started`
- `scheduler_job_error`
- `backup_success`
- `backup_failed`
- `backup_stale`
- `sqlite_quick_check_failed`
- `disk_low`
- `graph_degraded`
- `mcp_started`
- `mcp_auth_failed`
- `mcp_oauth_register`
- `cloudflare_public_down`
- `credential_unhealthy`
- `canary_leak`

### Alerting

Immediate owner Telegram alert when possible:

- bot restarted after crash loop
- startup health red
- SQLite quick_check failed
- backup older than 36 hours
- disk free below 5 GB
- Telegram send failure repeated 3 times
- Claude SDK auth/process failures repeated
- scheduler stopped or a critical job fails repeatedly
- Cloudflare public MCP down for more than 15 minutes when enabled
- external MCP auth abuse rate high
- credential probe invalid
- canary/log secret leak detection fires

Fallback when Telegram is unavailable:

- macOS notification from dead-man LaunchAgent
- optional email or SMS provider later
- visible terminal/log marker is not enough

For solo-dev, do not add PagerDuty. Add a smoke alarm that reaches the owner.

## "Immortality ladder": P0/P1/P2 implementation sequence

### P0: make it restartable and recoverable

These are the practical "do this first" items.

1. Add `scripts/health_check.py`.
   - Human and JSON output.
   - Checks launchd, Telegram, SQLite, backup age, disk, scheduler visibility, MCP if enabled.
2. Add Telegram `/status`.
   - Deterministic; no Claude call.
   - Shows green/yellow/red and the last red reason.
3. Add startup health digest.
   - Quiet on green, loud on yellow/red.
4. Add dead-man LaunchAgent.
   - Separate process.
   - No Claude import.
   - Pages owner if heartbeat stale.
5. Make backups safe.
   - Keep local raw backup with mode `0600`.
   - Store scrubbed copy in iCloud.
   - Write manifest.
   - Verify backup with `pragma quick_check`.
6. Run and document first restore drill.
   - Restore latest backup to temp path.
   - Verify key row counts.
7. Add LaunchAgents for external MCP and Cloudflare or formally install Cloudflare's service.
8. Pin external MCP package specs.
   - Replace `@latest` and floating `npx -y` specs with known versions or a manual update script.
9. Write the silence runbook into `docs/` or link this report from `codex/index.md`.

### P1: make failures idempotent and observable

1. Durable outbound ledger.
   - One table for attempted/sent/failed outbound messages.
   - Idempotency key per visible send.
2. Global proactive reservation.
   - One lock/lease in SQLite so heartbeat, reengage, calendar, reminders cannot collide.
3. Fix content-block SDK session fork risk.
   - Ensure media/document turns preserve the live session invariant.
4. Scheduler event listener.
   - Record job executed/error/missed events into ops events.
5. Capability health is live, not startup-only.
   - If Google creds are fixed, scheduler can register/resume jobs or `/status` tells owner restart is required.
6. Graph recovery contract.
   - SQLite graph outbox.
   - Backfill status.
   - "Graph is rebuildable" documented and tested.
7. Production Keychain enforcement.
   - `HIKARI_REQUIRE_KEYCHAIN=1` in LaunchAgents.
   - No silent memory-store fallback in production.
8. External MCP OAuth spec catch-up.
   - Validate `resource` / audience.
   - Add DCR caps and rate limits.
   - Add token pruning.

### P2: make it boring over months

1. Quarterly new-machine restore drill.
2. Storage growth report.
   - DB size, table row counts, attachment size, logs, backups.
3. Dependency update cadence.
   - Scheduled monthly update branch.
   - Run tests, health check, smoke boot.
   - Roll back by git commit and `uv.lock`.
4. Local dashboard or richer `/status`.
   - Last 24h events, next jobs, backup status, current model/auth state.
5. Optional Cloudflare hardening.
   - Access policy in front of MCP if compatible with connector clients.
   - Separate tunnel per environment.
6. Optional continuous backup.
   - Only if daily backup plus restore drills feels insufficient.
   - Keep the current simplicity unless real data loss risk appears.

## Suggested tests / drills

### Crash/restart drill

```bash
launchctl kickstart -k gui/$(id -u)/com.hikari.agent
```

Verify:

- startup health digest is sane
- `/status` is green/yellow with accurate reasons
- no duplicate proactive messages
- scheduler jobs exist
- a Telegram ping works

### Backup restore drill

```bash
LATEST=/path/to/latest/hikari.db
cp "$LATEST" /tmp/hikari-restore-test.db
sqlite3 /tmp/hikari-restore-test.db "pragma quick_check;"
sqlite3 /tmp/hikari-restore-test.db "select count(*) from messages;"
sqlite3 /tmp/hikari-restore-test.db "select count(*) from facts;"
sqlite3 /tmp/hikari-restore-test.db "select count(*) from reminders;"
```

Pass condition:

- quick_check is `ok`
- expected tables are readable
- row counts are plausible
- manifest commit matches a known repo state

### Telegram token drill

In a safe staging/foreground env, use an invalid token.

Verify:

- health check reports Telegram red
- startup does not claim healthy
- no phantom assistant rows are written for failed sends
- owner alert path works when token is restored

### Claude session drill

In a test DB copy:

- set the stored Claude session ID to a bogus value
- send a test turn
- verify runtime clears/retries or reports a clean SDK error
- verify SQLite memory is not lost

### Scheduler drill

- Temporarily force one job to raise in a test branch or local copy.
- Verify APScheduler listener records `scheduler_job_error`.
- Verify `/status` shows the failing job and last exception.

### Backup stale drill

- Temporarily point health check at an empty backup dir.
- Verify yellow/red health and owner notification.

### Disk pressure drill

- Set test warning threshold above current free disk.
- Verify `/status` and owner alert.
- Do not actually fill the disk.

### Cloudflare/MCP drill

- Stop `cloudflared`; local MCP should remain healthy and public MCP should go red.
- Stop MCP; public tunnel should report origin failure and local `:8765` should go red.
- Rotate bearer secret in test and confirm old bearer fails.
- Run OAuth connector reauth and verify tokens/audit rows.

### Credential drill

- Run `scripts.auth google status`.
- Revoke/regrant in a planned window.
- Confirm calendar/reminder jobs recover after restart.

### Graph rebuild drill

In a test copy:

- move `data/hikari.kuzu` aside
- start with `OPENROUTER_API_KEY` present
- run intended backfill script
- verify SQLite recall still worked during graph downtime

### New-machine drill

Quarterly:

- restore to a temporary macOS user or spare machine
- use a backup manifest commit
- `uv sync --frozen`
- restore DB
- configure minimal secrets
- run foreground boot
- send one Telegram ping

## Sources

Local project sources inspected:

- `AGENTS.md`
- `CLAUDE.md`
- `codex/index.md`
- `codex/top-system-review-and-roadmap-2026-05-23.md`
- `codex/deep-architecture-review-2026-05-23.md`
- `codex/security-review-2026-05-23.md`
- `codex/security-solo-dev-deep-dive-2026-05-23.md`
- `codex/2026-05-23-modernity-architecture-review.md`
- `agents/runtime.py`
- `agents/proactive.py`
- `agents/scheduler.py`
- `agents/telegram_bridge.py`
- `agents/background_listener.py`
- `agents/google_health.py`
- `agents/log_scrub.py`
- `mcp_external/server.py`
- `mcp_external/oauth.py`
- `mcp_external/launch.py`
- `mcp_external/_rate_limit.py`
- `storage/db.py`
- `storage/graph.py`
- `scripts/install_launchd.sh`
- `scripts/install_backup.sh`
- `scripts/backup.sh`
- `scripts/install_cloudflared.md`
- `scripts/auth.py`
- `scripts/migrate_secrets_to_keychain.py`
- `scripts/backfill_embeddings.py`
- `scripts/backfill_facts_to_graph.py`
- `scripts/regen_mcp_json.py`
- `scripts/validate_tool_registry.py`
- `.mcp.json`
- `pyproject.toml`
- `uv.lock`
- `config/engagement.yaml`
- `config/tools.yaml`
- `config/scopes.yaml`
- `docs/duckdb_mcp.md`

External official references:

- Apple, [Creating Launch Daemons and Agents](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html)
- Cloudflare, [Cloudflare Tunnel overview](https://developers.cloudflare.com/tunnel/)
- Cloudflare, [Run cloudflared as a service on macOS](https://developers.cloudflare.com/tunnel/advanced/local-management/as-a-service/macos/)
- Telegram, [Bot API: getting updates, getUpdates, deleteWebhook, getWebhookInfo](https://core.telegram.org/bots/api#getting-updates)
- APScheduler, [User guide](https://apscheduler.readthedocs.io/en/3.x/userguide.html)
- SQLite, [Online Backup API](https://www.sqlite.org/backup.html)
- SQLite, [VACUUM INTO](https://sqlite.org/lang_vacuum.html#vacuuminto)
- Model Context Protocol, [Authorization specification 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
- Anthropic, [Claude Agent SDK sessions](https://code.claude.com/docs/en/agent-sdk/sessions)
