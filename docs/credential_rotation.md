# Credential rotation

Procedure for rotating every secret Hikari uses. Do in order; some steps depend on prior ones.

## Google / Notion / GitHub OAuth

1. Revoke at provider console (Google Cloud, Notion Settings, GitHub Settings → Developer settings).
2. Run `uv run python scripts/auth.py google revoke` then `... grant`.
3. Same for `notion` and `github`.
4. Restart bot: `launchctl kickstart -k gui/$(id -u)/com.hikari.agent`

## HIKARI_MCP_SECRET (mcp_external bearer)

1. Generate a new value: `python -c "import secrets; print(secrets.token_urlsafe(32))"`
2. Update `.env`: `HIKARI_MCP_SECRET=<new value>`
3. Create a new oauth_token_hashes row: `uv run python -c "from storage import db; print(db.oauth_token_create(owner='cli-bearer'))"`
4. Revoke old: `uv run python -c "from storage import db; db.oauth_token_revoke('<old value>')"`
5. Restart bot + mcp_external service: `launchctl kickstart -k gui/$(id -u)/com.hikari.mcp`

## HIKARI_OAUTH_OWNER_PASSPHRASE (owner login cookie)

1. New passphrase in `.env`.
2. Restart bot.
3. Old cookie sessions invalidated.

## Cloudflared tunnel credential

1. `cloudflared tunnel rotate hikari-mcp`
2. Replace `~/.cloudflared/<UUID>.json` with the new credential file.
3. Restart `com.hikari.tunnel`: `launchctl kickstart -k gui/$(id -u)/com.hikari.tunnel`

## Telegram bot token

1. Talk to @BotFather → `/revoke` then `/token`.
2. New value in `.env` as `TELEGRAM_BOT_TOKEN`.
3. Restart bot: `launchctl kickstart -k gui/$(id -u)/com.hikari.agent`

## HIKARI_DEADMAN_BOT_TOKEN (separate channel)

1. Create a SEPARATE Telegram bot via @BotFather.
2. New value in `.env` and in `~/Library/LaunchAgents/com.hikari.deadman.plist`.
3. Reinstall: `bash scripts/install_deadman.sh`
   Or manually: `launchctl bootout gui/$(id -u)/com.hikari.deadman && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hikari.deadman.plist`

## Backup age key pair

1. The private key lives at `~/.config/hikari/backup_age.key` (600).
2. To rotate: move the old key aside, then `bash scripts/age_keygen.sh`.
3. Re-install the backup service: `bash scripts/install_backup.sh`
4. Previous encrypted backups can only be decrypted with the OLD private key — keep it archived.

## CLAUDE_CODE_OAUTH_TOKEN

1. `claude setup-token` — opens browser OAuth flow.
2. Paste the new token into `.env` as `CLAUDE_CODE_OAUTH_TOKEN`.
3. Restart bot.

## OPENROUTER_API_KEY (photos + LLM aux ops)

1. openrouter.ai/keys → revoke old → create new.
2. Update `.env`.
3. Restart bot.

## After any rotation

```bash
launchctl kickstart -k gui/$(id -u)/com.hikari.agent
```

If the mcp_external server or cloudflared tunnel is affected, restart those too:

```bash
launchctl kickstart -k gui/$(id -u)/com.hikari.mcp
launchctl kickstart -k gui/$(id -u)/com.hikari.tunnel
```
