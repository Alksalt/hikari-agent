# hikari-agent — operator runbook

Single-user Telegram agent on the Claude Agent SDK. Runs on a Max
subscription's $200/mo Agent SDK quota (no API-key billing). Sprint 6B
made the README the operator runbook: install, monitor, recover without
reading source.

---

## Quick start (1 min)

```bash
# clone + install
uv sync

# one-time: generate OAuth token tied to your Claude Max subscription
claude setup-token
# copy the printed token into .env as CLAUDE_CODE_OAUTH_TOKEN
# NEVER also set ANTHROPIC_API_KEY — that double-bills on top of Max

# secrets bootstrap
cp .env.example .env
# fill: CLAUDE_CODE_OAUTH_TOKEN, TELEGRAM_BOT_TOKEN, OWNER_TELEGRAM_ID
# optional: OPENROUTER_API_KEY (photos), DEEPL_API_KEY (translate),
#           HOME_TZ (scheduler), HOME_LAT/HOME_LON (weather fallback)

# (optional but recommended) migrate daemon-critical secrets to Keychain
uv run python -m scripts.auth google grant       # Google Workspace OAuth
uv run python -m scripts.auth notion grant       # Notion integration
uv run python -m scripts.auth github paste       # GitHub PAT
uv run python scripts/migrate_secrets_to_keychain.py

# foreground run for first-time verification
uv run hikari-agent
```

Send any message to your Telegram bot. Reply should arrive in Hikari's
voice. Anthropic console shows $0 API spend — quota deducts from Max.

---

## Service management (launchd)

Hikari runs as a per-user LaunchAgent. Five plists:

| Label                  | Purpose                                                   |
|------------------------|-----------------------------------------------------------|
| `com.hikari.agent`     | the bot itself; runs `uv run hikari-agent`                |
| `com.hikari.backup`    | daily encrypted backup at 03:00 local (Sprint 7F)         |
| `com.hikari.deadman`   | liveness monitor, fires every 5 min (Sprint 7F)           |
| `com.hikari.mcp`       | external MCP server (optional, Sprint 7/14)               |
| `com.hikari.tunnel`    | cloudflared tunnel for MCP server (optional, Sprint 7/14) |

Install plists (idempotent — re-running is safe):

```bash
# Core (required):
./scripts/install_launchd.sh

# Encrypted backup (requires age keypair — generate first):
bash scripts/age_keygen.sh    # one-time: generates ~/.config/hikari/backup_age.{key,pub}
./scripts/install_backup.sh

# Dead-man monitor (requires HIKARI_DEADMAN_BOT_TOKEN + OWNER_TELEGRAM_ID in env):
./scripts/install_deadman.sh

# External MCP server + cloudflared tunnel (optional, only if cross-device sync is needed):
./scripts/install_external_mcp_launchd.sh
./scripts/install_cloudflared_launchd.sh
```

Common ops:

```bash
# show service status + last exit code
launchctl print gui/$(id -u)/com.hikari.agent
launchctl print gui/$(id -u)/com.hikari.backup

# restart the bot (picks up code + env changes)
launchctl kickstart -k gui/$(id -u)/com.hikari.agent

# stop the bot
launchctl bootout gui/$(id -u)/com.hikari.agent

# foreground debug — pipes stderr to your terminal
launchctl bootout gui/$(id -u)/com.hikari.agent
uv run hikari-agent
```

---

## Log paths

| Path                                          | What it holds                                    |
|-----------------------------------------------|--------------------------------------------------|
| `~/Library/Logs/hikari.log`                   | launchd stdout (boot messages, scheduler init)   |
| `~/Library/Logs/hikari.err`                   | launchd stderr (uncaught tracebacks)             |
| `data/logs/hikari.log`                        | application log (rotating 20 MB × 5)             |
| `data/logs/mcp_external.log`                  | external-MCP server output (Phase 7/14; only if `mcp_external.enabled` in engagement.yaml) |
| `~/Library/Logs/hikari-backup.log`            | daily backup stdout                              |
| `~/Library/Logs/hikari-backup.err`            | daily backup stderr                              |

Recipes:

```bash
# tail the application log live
tail -f data/logs/hikari.log

# tail the launchd stderr — anything fatal lands here
tail -f ~/Library/Logs/hikari.err

# count ERRORs in the last hour
grep ERROR data/logs/hikari.log | tail -50

# inspect today's backup activity
tail -50 ~/Library/Logs/hikari-backup.log
```

---

## OAuth + credential setup

### Claude (Max subscription)

```bash
claude setup-token                              # opens browser for OAuth
# paste the printed token into .env as CLAUDE_CODE_OAUTH_TOKEN
```

### Telegram bot

1. Message `@BotFather` on Telegram, run `/newbot`, get the token.
2. Get your numeric user ID from `@userinfobot`.
3. Add to `.env`: `TELEGRAM_BOT_TOKEN=...`, `OWNER_TELEGRAM_ID=...`.

The bot is locked to that single user ID; anyone else gets silence.

### Google Workspace (Gmail / Calendar / Drive / Docs / Sheets / Slides)

```bash
uv run python -m scripts.auth google grant
# follow OAuth prompts in browser; tokens land in macOS Keychain
# or paste GOOGLE_WORKSPACE_{CLIENT_ID,CLIENT_SECRET,REFRESH_TOKEN} into .env
```

Google's testing-mode tokens expire after 7 days. `agents/google_health.py`
probes at boot and writes the result into `runtime_state`. If you see a
`google_workspace: refresh token UNHEALTHY` log line:

```bash
uv run python scripts/setup_google_oauth.py
launchctl kickstart -k gui/$(id -u)/com.hikari.agent
```

### Notion / GitHub

```bash
uv run python -m scripts.auth notion grant     # browser OAuth
uv run python -m scripts.auth github paste     # paste a GH PAT (repo + issues)
```

Both store in Keychain; `.env` fallbacks (`NOTION_TOKEN`,
`GITHUB_PERSONAL_ACCESS_TOKEN`) still work.

---

## Backup + restore

`scripts/backup.sh` (run nightly at 03:00 via `com.hikari.backup`) — Sprint 7F:

- Source: `data/hikari.db` (via sqlite3 `.backup` — WAL-safe), `.env`, `secrets/`, keychain export, `~/.cloudflared/`
- Destination: `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/alt-wiki/projects/hikari-agent/backups/hikari-YYYYMMDD.tar.age`
- Encrypted with [age](https://age-encryption.org/) using a recipient public key at `~/.config/hikari/backup_age.pub`
- Retains 14 most-recent encrypted archives; prunes older.

### First-time backup setup (Sprint 7F)

```bash
# 1. generate age keypair (one-time per machine)
bash scripts/age_keygen.sh
# output: ~/.config/hikari/backup_age.{key,pub}
# IMPORTANT: copy backup_age.key somewhere OFF this machine immediately

# 2. install the backup service
bash scripts/install_backup.sh

# manual test run
bash scripts/backup.sh
ls ~/Library/Mobile\ Documents/iCloud~md~obsidian/Documents/alt-wiki/projects/hikari-agent/backups/
```

### Restore drill

```bash
# DRY_RUN=1 walks through all steps without touching the filesystem:
DRY_RUN=1 bash scripts/restore.sh ~/path/to/hikari-YYYYMMDD.tar.age

# Live restore (decrypts + extracts to /tmp/hikari-restored/, then manual):
bash scripts/restore.sh ~/path/to/hikari-YYYYMMDD.tar.age
# Then follow the printed steps to copy files into place.
```

### New-machine restore

```bash
# 1. install repo + deps
git clone https://github.com/<you>/hikari-agent && cd hikari-agent && uv sync

# 2. restore from encrypted backup
bash scripts/restore.sh ~/path/to/hikari-YYYYMMDD.tar.age
# follow the steps printed; copy hikari.db, .env, secrets/ into place

# 3. re-grant OAuth
uv run python -m scripts.auth google grant
uv run python -m scripts.auth notion grant
uv run python -m scripts.auth github paste

# 4. install launchd
./scripts/install_launchd.sh
bash scripts/age_keygen.sh   # if new machine — generate fresh keypair
./scripts/install_backup.sh

# 5. send a /status message in Telegram — startup digest will surface anything still broken
```

### Dead-man monitor (Sprint 7F)

Runs every 5 min via `com.hikari.deadman`. Checks: agent running, DB mtime fresh,
backup fresh (<30h), MCP external alive, cloudflared tunnel running. Posts a
Telegram alert via a SEPARATE bot token if any check fails.

```bash
# test run (dry-run, no Telegram)
uv run python scripts/dead_man.py --dry-run

# install (requires HIKARI_DEADMAN_BOT_TOKEN + OWNER_TELEGRAM_ID in env)
bash scripts/install_deadman.sh
```

See `docs/credential_rotation.md` for rotation procedures.

---

## Health + `/status`

Sprint 6D installs a structured startup health probe. Every boot logs a
single `startup_health: {...}` line to `data/logs/hikari.log` covering:

| Check                  | Threshold                            |
|------------------------|--------------------------------------|
| `db_integrity`         | `PRAGMA quick_check` must return `ok` |
| `scheduler_jobs`       | at least 1 job registered            |
| `mcp_warm_pool`        | reachable (size is informational)    |
| `oauth_google`         | refresh-token exchange succeeds      |
| `graph_outbox_pending` | < 50 pending writes (Sprint 5D)      |
| `last_backup_age_h`    | ≤ 30 hours                           |
| `log_recent_errors`    | ≤ 5 ERROR lines in the last hour     |

If anything is degraded, the owner gets a single short DM with the
failing checks. Tune via `HIKARI_STARTUP_DIGEST=always|on_degrade|never`
(default `on_degrade`).

The `/status` Telegram command (Sprint 6A) returns the live equivalent:
uptime, silence window, scheduler job count + ids, MCP warm pool,
OAuth probe state, DB row counts (facts/messages/tasks/episodes), pending
approvals, cost today, proactive 7-day send count, graph_outbox stats.

---

## Recovery recipes

### Bot is silent (no reply to Telegram messages)

3-command triage:

```bash
# 1. is it running?
launchctl print gui/$(id -u)/com.hikari.agent | grep state

# 2. what's the last error?
tail -30 ~/Library/Logs/hikari.err

# 3. does the bot token still work?
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" | jq .
```

Then `launchctl kickstart -k gui/$(id -u)/com.hikari.agent` to restart.

### DB corruption

```bash
sqlite3 data/hikari.db "PRAGMA quick_check"
# if NOT ok, restore from the latest encrypted backup:
launchctl bootout gui/$(id -u)/com.hikari.agent
mv data/hikari.db data/hikari.db.broken
LATEST=$(ls -t ~/Library/Mobile\ Documents/iCloud~md~obsidian/Documents/alt-wiki/projects/hikari-agent/backups/hikari-*.tar.age | head -1)
bash scripts/restore.sh "$LATEST"
# follow restore.sh's printed steps to copy hikari.db into place
launchctl kickstart -k gui/$(id -u)/com.hikari.agent
```

### Scheduler hung / no proactive messages

```bash
# /status from Telegram shows current job list
# if empty, restart picks up jobs from agents/scheduler.py
launchctl kickstart -k gui/$(id -u)/com.hikari.agent
```

### MCP child process hung (Google / Notion / Playwright)

```bash
# warm pool evicts after the per-server TTL in config/tools.yaml.
# force-evict by restarting:
launchctl kickstart -k gui/$(id -u)/com.hikari.agent

# kill orphaned MCP processes (cosmetic — they exit when stdin closes):
pgrep -fl "google-workspace-mcp|notion-mcp-server|server-github|playwright" | head
```

### OAuth token revoked (Google 7-day testing-mode expiry)

```bash
uv run python scripts/setup_google_oauth.py
launchctl kickstart -k gui/$(id -u)/com.hikari.agent
```

---

## Telegram commands

| Command       | What it does                                              |
|---------------|-----------------------------------------------------------|
| `/help`       | list of registered commands                               |
| `/start`      | bootstrap message (Sprint 4 onwards)                      |
| `/silence`    | mute proactive messages for N minutes (default 60)        |
| `/unsilence`  | resume proactive messages immediately                     |
| `/tasks`      | list open tasks (memory-tracked open loops)               |
| `/cancel`     | cancel a pending in-flight tool call                      |
| `/cost`       | today's spend across Max + OpenRouter buckets             |
| `/memory`     | inspect / correct / forget memory + session search (5B)   |
| `/memory_diff`| SQLite vs Graphiti reconciliation (5D)                    |
| `/approvals`  | list pending gatekeeper approvals                         |
| `/proactive`  | `recent` / `why <id>` / `snooze <source> <duration>`      |
| `/status`     | structured health + activity dump (6A + 6D)               |
| `/tools`      | tool registry by capability group; `recent` / `policy`    |
| `/audit`      | audit log; `recent [N]` / `tools` / `approvals` / `id <id>`|
| `/settings`   | runtime settings; `get <key>` / `set <key> <value>`       |
| `/grab_stickers` | one-off: bulk-import a Telegram sticker pack into the local set |

---

## Credential rotation

Quick refs (full procedures in `docs/credential_rotation.md`):

| Secret                          | Rotate via                                          |
|---------------------------------|-----------------------------------------------------|
| `CLAUDE_CODE_OAUTH_TOKEN`       | `claude setup-token` → replace in `.env`            |
| `TELEGRAM_BOT_TOKEN`            | `@BotFather` → `/revoke` → `/token` → `.env`        |
| `HIKARI_DEADMAN_BOT_TOKEN`      | @BotFather (new bot) → `.env` → `install_deadman.sh`|
| Google Workspace refresh token  | `scripts.auth google grant`                         |
| Notion integration token        | `scripts.auth notion grant`                         |
| GitHub PAT                      | github.com/settings/tokens → `scripts.auth github paste` |
| `OPENROUTER_API_KEY`            | openrouter.ai/keys → `.env`                         |
| `HIKARI_MCP_SECRET` (Phase 7/14)| `secrets.token_urlsafe(32)` → `.env` → restart      |
| `HIKARI_OAUTH_OWNER_PASSPHRASE` | `openssl rand -base64 24` → `.env` → restart        |
| age backup key pair             | `bash scripts/age_keygen.sh` → `install_backup.sh`  |

After any rotation: `launchctl kickstart -k gui/$(id -u)/com.hikari.agent`.

---

## Test tiers + lint

```bash
uv run pytest -q                                  # default: ~1500 tests offline
uv run pytest -m "slow" -q                        # live-API tests (Whisper, etc.)
uv run pytest tests/persona/ -q                   # persona regression suite
uv run pytest tests/test_link_shelf_ssrf.py -q    # security regression sweep
uv run ruff check .
uv run python scripts/validate_tool_registry.py
uv run python scripts/validate_mcp_servers.py --skip apple_events,apple_shortcuts
uv run python scripts/regen_mcp_json.py --check
```

---

## Layout (current)

```
hikari-agent/
├── pyproject.toml
├── .env.example                # secret skeleton + per-host overrides
├── .mcp.json                   # external MCP servers (generated; do not hand-edit)
├── CLAUDE.md                   # always-loaded persona (Hikari constitution)
├── AGENTS.md                   # delegation map (subagents + utility tools)
├── README.md                   # this file
├── agents/
│   ├── runtime.py              # SDK client + 3-entrypoint split + MCP wiring
│   ├── telegram_bridge.py      # polling + owner lock + cockpit commands
│   ├── cockpit.py              # /help /status /tools /audit /settings (Sprint 6A)
│   ├── health.py               # startup health probe (Sprint 6D)
│   ├── hooks.py                # PreToolUse / PostToolUse / UserPromptSubmit
│   ├── proactive.py            # heartbeat + re-engagement + calendar heartbeat
│   ├── reflection.py           # daily reflection + session consolidation
│   ├── scheduler.py            # APScheduler job wiring
│   ├── injection_guard.py      # wrap_untrusted delimiters + canary detection
│   ├── google_health.py        # boot-time OAuth probe
│   └── subagents/              # wiki / drive_gmail / notion / research / github
├── tools/
│   ├── _annotations.py         # MCP ToolAnnotations (Sprint 6F)
│   ├── _tools_yaml.py          # registry loader, single source of truth
│   ├── _registry.py            # auto-discovery for in-process tools
│   ├── _lazy.py                # lazy_tool builder (link_shelf etc.)
│   ├── gatekeeper.py           # CONFIRM-SEND approval lifecycle
│   ├── gatekeeper_can_use_tool.py  # SDK can_use_tool + truthful previews (Sprint 6C)
│   ├── memory/                 # recall, remember, task_*, update_core_block, session_search
│   ├── wiki/                   # read, search, list, append, backlinks, tree
│   ├── link_shelf/             # save (SSRF-hardened), search, list, update, delete
│   ├── apple_notes/            # create, read, search (macOS osascript)
│   ├── reminders/              # create, list, snooze, cancel (DB + APScheduler)
│   ├── decision_log/           # capture, resolve (calibration ledger)
│   ├── day_receipt/            # add, today, week, search, set_note, delete, print
│   ├── codex/                  # list_reports, read_report
│   ├── photos/                 # generate_photo (OpenRouter Flux)
│   ├── dispatch/               # dispatch_claude_session (subagent fanout)
│   ├── router/                 # tool_search (BM25 over deferred tools)
│   └── calc/                   # calc, python_run (sandboxed compute)
├── storage/
│   ├── db.py                   # schema + helpers (idempotent ALTER inside _migrate fns)
│   ├── retrieval.py            # Park et al. scoring
│   └── graph.py                # Graphiti outbox drain worker (Sprint 5D)
├── config/
│   ├── tools.yaml              # tool registry (gate / access_mode / annotations source)
│   └── engagement.yaml         # tunables (typing, proactive, approvals, scheduling)
├── scripts/
│   ├── auth.py                 # Keychain-backed credential grants
│   ├── backup.sh               # nightly SQLite backup → iCloud
│   ├── install_launchd.sh      # com.hikari.agent plist installer
│   ├── install_backup.sh       # com.hikari.backup plist installer
│   ├── setup_google_oauth.py   # one-time Google consent flow
│   ├── migrate_secrets_to_keychain.py  # .env → Keychain shim
│   ├── regen_mcp_json.py       # rebuild .mcp.json from tools.yaml (pin validator)
│   ├── validate_tool_registry.py
│   └── validate_mcp_servers.py
└── tests/                      # 1500+ tests; default `pytest -q` is offline
```

---

## Architecture notes

- **Agent loop:** Sonnet 4.6 primary, Haiku 4.5 fallback (`fallback_model`). Sessions resume via SQLite.
- **Memory:** SQLite with `core_blocks`, bi-temporal `facts` (`valid_to` / `superseded_by`), `episodes`, `tasks`, `entities`, `character_thoughts`, `runtime_state`, FTS5 BM25. Park et al. retrieval scoring (recency × importance × relevance). Graphiti outbox + drain (Sprint 5D) makes the graph backend optional and durable.
- **Hooks:** `UserPromptSubmit` injects core_blocks + open tasks + top-8 retrieved hits. `PostToolUseFailure` logs failures. `PostToolUse` `untrusted_output` wrap defends against prompt injection from external tool results.
- **Approvals:** one canonical lifecycle via `tools/gatekeeper.py`. Destructive Google Workspace writes (gmail_send, delete_calendar_event, drive_delete_file, etc.) route through `CONFIRM-SEND` (Sprint 4C + 6C). Approval previews preserve critical fields in full (Sprint 6C).
- **External MCP servers:** every bucket-3 package pinned in `config/tools.yaml`; `scripts/regen_mcp_json.py` refuses to write `.mcp.json` if any package floats to `@latest` (Sprint 6E).

---

## Risks / known compromises

- **Max SDK quota** ($200/mo): heavy heartbeat usage can exhaust quota. Mitigation: prompt caching on persona blocks (auto-enabled by SDK), `max_budget_usd` cap per turn, `max_turns` cap.
- **Google OAuth testing-mode expiry**: refresh tokens die after 7 days in Testing-mode apps. Boot probe surfaces this loudly; rotation = re-run `scripts/setup_google_oauth.py`.
- **Graphiti is optional**: set `GRAPHITI_ENABLED=false` to skip the 30-s outbox drain worker. Outbox rows still accumulate in SQLite; they just don't get pushed to the graph. Reconcile with `scripts/reconcile_graph.py` when you re-enable.
- **macOS Automation prompts**: first call to apple_events / apple_shortcuts / apple_notes triggers a system permission prompt. Accept in System Settings → Privacy & Security → Automation. There is no CLI workaround.
- **Anthropic content safety** may refuse explicit material from `STAGES.md`. That content has to migrate to a separate OpenRouter route if needed — TBD.
- **Single-user assumption**: every command, every hook, every gate checks `OWNER_TELEGRAM_ID`. Removing that lock would require revisiting half the codebase. Don't.
