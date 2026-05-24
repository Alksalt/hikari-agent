# Second-Pass Ops / Production / Immortality Review - 2026-05-24

Scope: current working tree of `/Users/ol/agents/hikari-agent` as of 2026-05-24. I treated existing `codex/*.md` material as prior context only. The requested prior files are currently deleted in the working tree, so I read them from `HEAD` with `git show HEAD:...` and re-checked current behavior from source, tests, config, docs, and live verification commands.

## 1. Current-state summary

The production spine is much stronger than the old priors described. The repo now has launchd installers for the bot, backup job, dead-man monitor, external MCP server, and Cloudflare tunnel; a WAL-safe SQLite backup script using `.backup`; an age-encrypted archive format; a restore script; startup health collection; a Telegram `/status`; credential-rotation docs; package pinning for external MCP processes; and CI coverage in `.github/workflows/ci.yml`.

The live state is not yet at "immortal" quality. `sqlite3 data/hikari.db "PRAGMA quick_check;"` returned `ok`, but the configured iCloud backup directory currently contains only legacy plaintext `hikari-*.db` files from May 20-23 and no `hikari-*.tar.age` archives. `uv run python scripts/dead_man.py --dry-run` reports `backup_fresh: FAIL` and `cloudflared: FAIL`. The main restore path also has a correctness bug that deletes its extracted payload before an operator can use it.

Targeted ops tests passed: `uv run python -m pytest tests/test_backup_encryption.py tests/test_dead_man_checks.py tests/test_health.py tests/test_google_health.py tests/test_smoke.py -q` -> `99 passed, 1 skipped, 1 warning`. `uv run python scripts/regen_mcp_json.py --check` -> `.mcp.json is up to date.`

## 2. Findings, ordered P0/P1/P2/P3

### P0

No current P0 found in this pass. The DB opens and passes `PRAGMA quick_check`; there is a backup mechanism and multiple launchd definitions in tree. The P1s below are still enough to make disaster recovery unreliable.

### P1

1. **Restore script wipes the decrypted restore payload before it can be copied.**
   `scripts/restore.sh:20-26` installs a cleanup trap that removes `TMP_ROOT` on normal exit, while `scripts/restore.sh:57-83` decrypts/extracts and then prints manual copy instructions. The final warning at `scripts/restore.sh:84` says the temp root will be wiped on shell exit, but the script exits immediately after printing it. README recovery paths rely on this script (`README.md:192-211`, `README.md:289-299`). In a real restore, the operator can end up with no extracted `hikari.db`, `.env`, `secrets/`, keychain export, or `.cloudflared` directory to copy.

2. **Encrypted backup is documented as the current production state, but the live backup directory still has only legacy plaintext DB snapshots, and the backup writer can bless partial archives.**
   README documents age archives at `README.md:169-174`, but the configured backup directory currently lists only `hikari-20260520.db` through `hikari-20260523.db`, all mode `rw-r--r--`, and no `.tar.age`. The dead-man dry-run confirms `backup_fresh: FAIL`. Separately, `scripts/backup.sh:28-33` skips the day if the final `.tar.age` exists, and `scripts/backup.sh:100` writes age output directly to that final path. A crash or interrupted `age` write can leave a corrupt same-day archive that future runs skip. The SQLite snapshot itself uses `.backup` at `scripts/backup.sh:52-63`, which is the right primitive for a live DB, but the final archive needs atomic temp-then-rename plus a decrypt/untar/quick_check smoke.

3. **Google Calendar mirror scheduling is decided before the startup OAuth probe updates health state.**
   `agents/telegram_bridge.py:2374-2377` builds and starts the scheduler, then the Google refresh-token probe runs at `agents/telegram_bridge.py:2393-2409`. The GCal sync job is registered only if `_calendar_creds_healthy()` returns true during scheduler construction (`agents/scheduler.py:62-78`), and that helper trusts any existing `runtime_state.calendar_heartbeat_healthy` value (`agents/scheduler.py:386-408`). A stale `0:*` row can suppress `reminders_gcal_sync` even when the fresh probe succeeds; a stale `1` can register it before the fresh probe fails. Result: calendar mirror recovery can require an extra restart or silently accumulate pending mirrors.

4. **Dead-man monitor liveness semantics are not production-grade yet.**
   `scripts/dead_man.py:34-47` checks only whether `launchctl list` contains `com.hikari.agent`, not whether the job has a PID, current state, or recent nonzero exit. `scripts/dead_man.py:49-53` treats DB mtime under 30 minutes as bot liveness, which can false-page during quiet periods and false-green if any unrelated writer touches the DB. `scripts/dead_man.py:107-113` unconditionally requires external MCP and Cloudflare to be up, even when those optional services are not installed or not intended for a machine. `scripts/dead_man.py:83-99` does not check Telegram HTTP status and has no debounce, so a persistent failure can alert every 5 minutes.

5. **Outbound durability is only partially implemented for text sends.**
   `agents/messaging.py:111-130` inserts `media_outbox` before Telegram send, but the idempotency key includes `int(time.time() * 1000)`, so it is not stable across retries or restarts despite the comment. `agents/messaging.py:156-184` marks failed sends and persists assistant text only after Telegram confirms send. The boot drain is photo-only (`agents/telegram_bridge.py:197-247`), while the table supports all kinds (`storage/db.py:4227-4315`). If the process crashes after Telegram send but before DB append, the chat gets the message but memory can miss it; if it crashes before send, a pending text row has no replay worker.

6. **External MCP OAuth still lacks resource/audience binding.**
   The server advertises protected-resource discovery in the 401 challenge (`mcp_external/launch.py:168-180`), but authorization and token exchange do not require or persist a `resource` parameter (`mcp_external/oauth.py:324-386`, `mcp_external/oauth.py:516-573`), and access-token validation accepts any locally valid access token without checking audience/resource (`mcp_external/launch.py:138-149`). Current MCP authorization guidance requires clients to send RFC 8707 `resource` and servers to validate that tokens were issued for the MCP server. This is both a spec-compatibility risk and a future confused-deputy risk if the auth surface grows.

### P2

7. **`/status` is documented as the live equivalent of startup health, but it is not.**
   README says `/status` returns the live equivalent of startup health at `README.md:263-266`. The actual status renderer (`agents/cockpit.py:240-336`) reports uptime, silence, scheduler jobs, warm MCP names, OAuth state, row counts, cost, proactive count, and graph outbox pending. It does not call `collect_startup_report()` and omits DB quick_check, backup age, recent log errors, media outbox pending, external MCP, Cloudflare, dead-man state, and SDK pool readiness. Startup health itself covers only the checks listed in `agents/health.py:219-242`.

8. **Startup health is useful but incomplete for ops recovery.**
   `agents/health.py:232-240` checks DB integrity, scheduler jobs, warm pool, Google OAuth, graph/media outboxes, backup age, and log errors. It does not verify that `com.hikari.agent`, `com.hikari.mcp`, `com.hikari.tunnel`, or `com.hikari.deadman` are loaded/running; does not decrypt the latest backup; does not check that `reminders_gcal_sync` is registered after a healthy Google probe; and runs before `_sdk_pool.startup()` (`agents/telegram_bridge.py:2419-2458`), so the startup digest cannot detect SDK pool startup failure.

9. **Launchd production path does not enforce Keychain-backed secret storage.**
   The main launchd installer sets only `PATH` (`scripts/install_launchd.sh:72-76`). `auth/store.py:89-112` falls back to `MemoryStore` unless `HIKARI_REQUIRE_KEYCHAIN=1` is set. That means a production launch can silently run without durable Keychain token storage after a keyring failure. `scripts/migrate_secrets_to_keychain.py` documents that migration is still advisory rather than wired into the bridge.

10. **Cloudflare tunnel install checks only that `~/.cloudflared` exists.**
    `scripts/install_cloudflared_launchd.sh:39-44` accepts any existing `~/.cloudflared` directory, but Cloudflare's service docs expect a usable `config.yml` plus tunnel credentials. The installer does not validate `config.yml`, `credentials-file`, tunnel UUID/name, public hostname route, or local origin URL before bootstrapping `com.hikari.tunnel`.

11. **Credential-rotation docs mix static bearer and hashed bearer procedures.**
    `docs/credential_rotation.md:12-18` says to rotate `HIKARI_MCP_SECRET` by both replacing the env var and creating/revoking `oauth_token_hashes` rows. In code, the static service-token path compares the presented bearer token directly to `HIKARI_MCP_SECRET` (`mcp_external/launch.py:117-123`), while the hashed bearer path is separate (`mcp_external/launch.py:125-136`). The runbook should separate "static emergency service token" from "hashed bearer token" rotation.

12. **Telegram polling intentionally drops pending updates, but the ops docs do not call that out.**
    `agents/telegram_bridge.py:2496-2500` runs polling with `drop_pending_updates=True`. Telegram's Bot API documents that pending updates can be dropped with this flag. This may be deliberate for stale-message hygiene, but it means messages sent while the bot is down can be discarded on restart. README recovery docs do not currently state that tradeoff.

### P3

13. **Cloudflare docs/config examples still have domain drift.**
    Runtime config uses `https://hikari.alksalt.com` (`config/engagement.yaml`), while local tunnel docs and smoke-test text still include placeholder-style hostnames in places (`scripts/install_cloudflared.md`, `scripts/install_cloudflared_launchd.sh:71`). This is low severity, but it slows new-machine recovery because the operator has to infer the real endpoint.

14. **Dead-man token is copied into a LaunchAgent plist.**
    `scripts/install_deadman.sh` writes `HIKARI_DEADMAN_BOT_TOKEN` and `OWNER_TELEGRAM_ID` into `~/Library/LaunchAgents/com.hikari.deadman.plist` and chmods it 600. That is acceptable for a solo-user LaunchAgent, but it should be called out as a local secret-bearing file in backup/rotation docs.

## 3. Previously reported issues that now look closed

- **No supervision for core services:** mostly closed. `scripts/install_launchd.sh`, `scripts/install_external_mcp_launchd.sh`, `scripts/install_cloudflared_launchd.sh`, `scripts/install_backup.sh`, and `scripts/install_deadman.sh` now exist with launchd plists.
- **No encrypted backup story:** partially closed. `scripts/backup.sh`, `scripts/install_backup.sh`, `scripts/age_keygen.sh`, `scripts/restore.sh`, and backup tests exist. Live backup state and restore correctness remain open.
- **No dead-man monitor:** closed as an implementation gap. The monitor and tests exist; its semantics need hardening.
- **No startup digest:** closed. `agents/health.py` and the bridge startup path emit a structured health report and optional owner DM.
- **No `/status`:** closed at the basic operator-summary level. It exists; it is just not equivalent to startup health.
- **External MCP process not supervised:** mostly closed. `com.hikari.mcp` launchd installer/plist exists.
- **Cloudflare tunnel not supervised:** mostly closed. `com.hikari.tunnel` launchd installer/plist exists.
- **Floating external MCP packages:** closed. `config/tools.yaml`, `.mcp.json`, and `scripts/regen_mcp_json.py --check` show pinned packages and up-to-date generated config.
- **No CI smoke path:** closed. `.github/workflows/ci.yml` now runs lint, tests, eval layers, registry validation, MCP JSON validation, and MCP server validation.
- **Google refresh-token expiry only discovered by user-facing failures:** partially closed. Startup probing exists, and Google's documented testing-mode 7-day refresh-token expiry is referenced in recovery docs. The scheduler ordering bug keeps the closeout incomplete.

## 4. New regressions or contradictions

- README says encrypted `.tar.age` backups are the destination and restore source, but the live backup directory currently contains only legacy plaintext `.db` snapshots and dead-man reports stale backup.
- README says `/status` is the live equivalent of startup health; code paths are separate and `/status` omits several startup checks.
- `scripts/restore.sh` says manual copy is required from the extracted directory, but the cleanup trap removes that directory when the script exits.
- Credential rotation conflates `HIKARI_MCP_SECRET` static bearer rotation with hashed bearer-token rows.
- The bridge comment says the startup OAuth probe populates state before scheduler gating, but the actual order is scheduler first, probe second.
- Cloudflare docs/scripts mix a placeholder smoke-test URL with the configured production `public_base_url`.

## 5. Missing tests / suggested verification

- Add a restore integration test that decrypts/extracts a tiny age archive, lets `scripts/restore.sh` exit, and asserts the operator can still access the extracted files. This would catch the current trap bug.
- Add a backup atomicity test: simulate a pre-existing corrupt same-day `.tar.age` and ensure the next backup does not silently skip it, or write to a temp file and rename only after decrypt/untar/quick_check succeeds.
- Add a launch order test for `post_init`: stale `runtime_state.calendar_heartbeat_healthy=0:*` plus a successful probe should still result in `reminders_gcal_sync` being registered.
- Add dead-man parser tests against realistic `launchctl print` or `launchctl list` outputs, including loaded-but-crashed, optional external MCP disabled, optional Cloudflare disabled, Telegram non-200, and debounce behavior.
- Add `/status` parity tests that assert the command includes the same critical keys as `collect_startup_report()` or explicitly documents the narrower surface.
- Add text/sticker `media_outbox` replay/reconciliation tests, or explicitly mark non-photo rows aborted at boot so they do not masquerade as durable queue entries.
- Add MCP OAuth tests for RFC 8707 `resource` on authorize/token requests, token audience storage, and resource/audience validation at request time.
- Run a live quarterly restore drill: produce an encrypted archive, decrypt it on a temp machine/path, run `PRAGMA quick_check` on the restored DB, import only the intended secret material, reinstall launchd jobs, and verify `/status`, dead-man dry-run, external MCP over the tunnel, and a real Telegram round trip.

## 6. Sprint or roadmap implications

The next ops sprint should prioritize recovery correctness before feature work: fix restore extraction lifetime, make encrypted backups atomic and verifiable, get a fresh `.tar.age` archive into the live backup directory, and make dead-man report real process health without paging for optional services. Those are the "can I recover tomorrow morning?" items.

After that, unify startup health and `/status`, reorder the Google OAuth probe before scheduler construction, and decide whether text outbound replay is required or whether text rows should be scoped out of the durable media ledger. The MCP OAuth resource/audience gap should stay near the top of the external-surface roadmap because the current code is behind an auth wall but not yet aligned with modern MCP authorization expectations.

Credential work is a smaller but worthwhile follow-up: make Keychain enforcement explicit for production launchd, separate static bearer vs hashed bearer rotation docs, and document all secret-bearing local files (`.env`, backup private key, dead-man plist, Cloudflare credentials, keychain export).

## 7. Sources used

Local source/docs/config:

- `README.md`
- `scripts/install_launchd.sh`
- `scripts/install_backup.sh`
- `scripts/backup.sh`
- `scripts/restore.sh`
- `scripts/dead_man.py`
- `scripts/install_deadman.sh`
- `scripts/install_external_mcp_launchd.sh`
- `scripts/install_cloudflared_launchd.sh`
- `scripts/launchd_mcp_external.plist`
- `scripts/launchd_cloudflared.plist`
- `scripts/install_cloudflared.md`
- `agents/telegram_bridge.py`
- `agents/scheduler.py`
- `agents/health.py`
- `agents/cockpit.py`
- `agents/messaging.py`
- `storage/db.py`
- `mcp_external/launch.py`
- `mcp_external/oauth.py`
- `auth/store.py`
- `docs/credential_rotation.md`
- `config/engagement.yaml`
- `config/tools.yaml`
- `.mcp.json`
- `.github/workflows/ci.yml`
- tests under `tests/test_backup_encryption.py`, `tests/test_dead_man_checks.py`, `tests/test_health.py`, `tests/test_google_health.py`, `tests/test_smoke.py`, and `tests/test_send_and_persist_api.py`
- Prior context read from `HEAD`: `codex/ops-production-runbook-2026-05-23.md`, `codex/2026-05-23-modernity-architecture-review.md`, `codex/security-solo-dev-deep-dive-2026-05-23.md`

Verification commands:

- `sqlite3 data/hikari.db "PRAGMA quick_check;"` -> `ok`
- `ls -lt ~/Library/Mobile\ Documents/iCloud~md~obsidian/Documents/alt-wiki/projects/hikari-agent/backups` -> legacy plaintext `.db` backups only, latest `hikari-20260523.db`
- `uv run python scripts/dead_man.py --dry-run` -> `backup_fresh` and `cloudflared` failed
- `uv run python scripts/regen_mcp_json.py --check` -> `.mcp.json is up to date.`
- `uv run python -m pytest tests/test_backup_encryption.py tests/test_dead_man_checks.py tests/test_health.py tests/test_google_health.py tests/test_smoke.py -q` -> `99 passed, 1 skipped, 1 warning`

Official external references:

- Apple Developer, [Creating Launch Daemons and Agents](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html)
- Apple Developer, [Scheduling Timed Jobs](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/ScheduledJobs.html)
- Cloudflare Docs, [Run as a service on macOS](https://developers.cloudflare.com/tunnel/advanced/local-management/as-a-service/macos/)
- Cloudflare Docs, [Useful terms for locally-managed tunnels](https://developers.cloudflare.com/tunnel/advanced/local-management/local-tunnel-terms/)
- Cloudflare Docs, [Tunnel health monitoring](https://developers.cloudflare.com/tunnel/monitoring/)
- Telegram, [Bot API](https://core.telegram.org/bots/api)
- SQLite, [Online Backup API](https://www.sqlite.org/backup.html)
- Model Context Protocol, [Authorization - 2025-06-18](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization)
- Google Developers, [Using OAuth 2.0 to Access Google APIs](https://developers.google.com/identity/protocols/oauth2)
