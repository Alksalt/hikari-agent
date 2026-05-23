# Security Solo-Dev Deep Dive - Hikari Agent

Date: 2026-05-23

This is a practical follow-up to `codex/security-review-2026-05-23.md`.

The previous report used normal security-review severity language. This one recalibrates for the actual deployment: a solo developer running a single-user Telegram companion bot, with local tools, personal cloud tokens, an Obsidian vault, and a read-only external MCP exposed through Cloudflare.

## Solo-Dev Threat Model

### In Scope

The risks that matter:

1. Malicious third-party content: email, web pages, Drive docs, Notion pages, attachments, transcripts, or wiki/memory text that tells the model to call tools.
2. Local secret exposure: `.env`, SQLite databases, iCloud vault backups, Google refresh tokens, GitHub PAT, Notion token, Telegram bot token.
3. Supply-chain surprise: external MCP packages launched by `npx`/`uvx` at runtime with local user privileges.
4. Public external MCP noise: unauthenticated internet clients hitting OAuth/DCR/discovery endpoints.
5. Accidental self-harm: approving a hidden or badly summarized tool call.

### Out of Scope

Not worth building right now:

1. Multi-user RBAC, roles, teams, admin panels, or tenant isolation.
2. Compliance-style audit log retention, SIEM forwarding, formal incident workflows.
3. Heavy web-app hardening beyond the small external MCP surface.
4. Full database-at-rest encryption if the local macOS account is trusted.
5. Enterprise secret-management ceremonies if a simpler Keychain/1Password path works.

The right strategy is thin controls at the exact boundaries where the agent can hurt you.

## Realistic Attack Chains

### Chain A: Prompt Injection to Tool Action

Path:

1. Hikari reads untrusted content from Gmail, Drive, web, Notion, a transcript, or an uploaded file.
2. The content asks the model to send/delete/upload/run something.
3. A gated tool fires.
4. The approval prompt truncates or poorly summarizes the real arguments.
5. You type `CONFIRM-SEND`.

Relevant code:

- `agents/hooks.py:620-627` truncates defer summaries.
- `tools/gatekeeper_can_use_tool.py:59-66` truncates gatekeeper summaries.
- `tools/approvals.py:129-136` sends the approval prompt.
- `tools/approvals.py:330-429` executes the original full args after approval.

Solo-dev verdict:

This is the highest-value fix. Not because you need enterprise approval workflow, but because "show me what I am approving" is the whole safety mechanism. If the preview lies by omission, the gate is theater.

Minimum fix:

- Show full destination-critical fields.
- Never truncate executable code, recipients, file paths, object IDs, delete counts, or message destinations.
- For huge bodies, show a safe preview plus exact body length and a local preview file path.
- If the approval preview itself must be truncated, make the action unapprovable until inspected.

Do not build:

- Multi-step approval chains.
- Admin roles.
- Separate policy engine.

### Chain B: Untrusted Attachment Escapes the Wrapper

Path:

1. You upload or forward a `.txt`, `.md`, `.html`, `.json`, `.py`, `.js`, `.yaml`, or similar document.
2. The bridge inlines it into the model prompt.
3. The file contains `<<<HIKARI_UNTRUSTED_END>>>`.
4. The manual wrapper treats the forged delimiter as real, and following text appears outside the untrusted block.

Relevant code:

- `agents/telegram_bridge.py:986-991`
- `agents/telegram_bridge.py:1007-1012`
- `agents/injection_guard.py:92-138`
- `tests/test_security.py:55-71`

Solo-dev verdict:

Fix this now. It is tiny and aligned with your existing architecture. No new layer needed.

Minimum fix:

- Replace the hand-built wrappers in `telegram_bridge.py` with `injection_guard.wrap_untrusted("telegram_document", text)`.
- Add one regression test with forged begin/end delimiters in an uploaded text/HTML payload.

### Chain C: Floating MCP Package Gets Updated or Compromised

Path:

1. The bot starts a bucket-3 external MCP server.
2. `npx -y` or `uvx --from` resolves the newest package version.
3. That package runs as your local user.
4. Some servers receive high-value tokens or local automation access.

Relevant code:

- `config/tools.yaml:50-62` Google Workspace MCP with Google credentials.
- `config/tools.yaml:64-80` Notion/GitHub MCPs with tokens.
- `config/tools.yaml:82-95` Playwright and Apple Events.
- `config/tools.yaml:98-104` Apple Shortcuts.
- `config/tools.yaml:115-129` DuckDB MCP.

Solo-dev verdict:

This is worth fixing, but not with a huge system. Pin versions. That is it.

Minimum fix:

- Replace floating package specs with exact versions or immutable Git SHAs.
- Remove `@latest`.
- Prefer a small update script you run intentionally over live package resolution at daemon startup.

Do not build:

- Internal package mirrors.
- Complex allowlisted dependency proxies.
- Per-package notarization workflows.

### Chain D: Broad Local-Read Tools Touch Secrets

Path:

1. A model call gets access to a broad tool: DuckDB, `python_run`, or Playwright.
2. The tool can read or transmit local data beyond the intended task.
3. Secrets or private memory are exposed through stdout, browser navigation, query results, or a remote page.

Relevant code:

- `config/tools.yaml:414-419` gates `python_run`.
- `tools/calc/python_run.py:57-74` runs sandboxed Python.
- `tools/calc/_shared.py:122-184` permits default file reads with only selective denies.
- `config/tools.yaml:610-616` exposes DuckDB wildcard ungated.
- `docs/duckdb_mcp.md:38-42` tells the agent to attach local SQLite paths.
- `config/tools.yaml:594-600` exposes Playwright wildcard ungated.

Solo-dev verdict:

Split this into three choices:

1. `python_run`: keep it, because it is useful and already defer-gated. Fix approval previews first. A deny-by-default file sandbox is nice but not urgent if you can clearly see the full code before approving.
2. DuckDB: do not leave a general SQL/file-read-capable engine always available. Make it opt-in, gated, or replace it with narrow analytics helpers. This is a small config/registry change, not a big security layer.
3. Playwright: if you do not use it in normal chat, make it opt-in or gated. A browser automation tool is a network egress path, even without explicit API keys.

Minimum fix:

- Gate or opt-in DuckDB.
- Gate or opt-in Playwright if not part of normal companion behavior.
- Keep `python_run` but require full code preview.

Do not build:

- A full container platform for this bot.
- A general policy language for every local read.

### Chain E: Database Backup Leaks Tokens and Memory

Path:

1. `scripts/backup.sh` copies `data/hikari.db` to the Obsidian iCloud vault.
2. The database contains personal memory and external MCP OAuth access/refresh tokens.
3. iCloud sync, another device, an Obsidian plugin, or a local file-read tool gets the backup.

Relevant code:

- `scripts/backup.sh:13-16`
- `scripts/backup.sh:23-25`
- `scripts/backup.sh:41-49`
- `storage/db.py:407-421`
- `storage/db.py:3113-3129`
- `storage/db.py:3132-3147`

Solo-dev verdict:

The full enterprise answer is "hash tokens, encrypt all backups, rotate on restore." The solo-dev answer is simpler:

1. Stop syncing raw live auth tokens to iCloud.
2. Keep the backup useful for memory restore.

Minimum fix:

- Make the backup script write a scrubbed copy for iCloud: delete `oauth_tokens`, maybe `approvals` pending args, then vacuum the temp copy.
- Set `chmod 600` on backup files.
- Hash OAuth tokens later if you keep exposing the external MCP to custom connectors.

Do not build:

- Full encrypted database storage unless you genuinely want it.
- A complex key rotation subsystem.

### Chain F: Public OAuth/DCR Endpoint Gets Internet Trash

Path:

1. `mcp_external.enabled: true` exposes a public Cloudflare URL.
2. OAuth discovery and dynamic client registration are reachable without auth.
3. Random clients register lots of metadata or hit auth endpoints.

Relevant code:

- `config/engagement.yaml:524-559`
- `mcp_external/oauth.py:227-260`
- `storage/db.py:3010-3026`
- `mcp_external/launch.py:100-104`
- `mcp_external/launch.py:262-266`

Solo-dev verdict:

This is not your main risk. The external MCP tools are read-only, the server binds to `127.0.0.1`, and actual tool access needs bearer/OAuth. The unauthenticated DCR endpoint is mostly a nuisance/DoS surface.

Minimum fix:

- Add small per-IP rate limiting to `/register`.
- Cap request body size, redirect URI count, URI length, and client name length.
- Add app-level trusted-host checking to match the FastMCP host allowlist.

Do not build:

- Full OAuth client-management UI.
- Manual client approval unless open DCR starts causing real noise.

### Chain G: Local Apple Automation Side Effects

Path:

1. Prompt injection asks for a calendar/reminder/shortcut action.
2. Apple Events and Shortcuts wildcard tools are ungated.
3. Local reminders/calendar items are created/deleted, or a shortcut runs.

Relevant code:

- `config/tools.yaml:578-592`
- `tests/test_destructive_tool_gating.py:158-219`

Solo-dev verdict:

Do not overreact to calendar/reminder writes. For your setup, those are mostly clutter and they keep reminder mirroring ergonomic.

Shortcuts are different. Depending on installed shortcuts, they can become arbitrary local automation.

Minimum fix:

- Keep Apple Reminders/Calendar ungated if you accept the clutter risk.
- Gate Apple Shortcuts, or allowlist only specific shortcut names.
- Document the decision in the test comments so future-you remembers it is a tradeoff, not an oversight.

## Practical Priority List

### Fix Now

These are small and high-signal.

1. Attachment wrapper escape bug.
   - Effort: small.
   - Payoff: closes a direct prompt-injection escape.
   - Files: `agents/telegram_bridge.py`, one test.

2. Approval preview truthfulness.
   - Effort: small-to-medium.
   - Payoff: protects every gated action.
   - Files: `agents/hooks.py`, `tools/gatekeeper_can_use_tool.py`, approval tests.

3. Pin external MCP package versions.
   - Effort: small.
   - Payoff: removes surprise runtime code changes.
   - Files: `config/tools.yaml`, `.mcp.json` generator/tests if generated.

4. Make DuckDB opt-in or gated.
   - Effort: small if config-only.
   - Payoff: removes an always-on local file-read class.
   - Files: `config/tools.yaml`, tests expecting DuckDB in allowlist.

### Fix Soon

Worth doing, but not before the above.

5. Scrub iCloud backups.
   - Effort: medium.
   - Payoff: protects tokens/private memory if backups spread.
   - Files: `scripts/backup.sh`, backup test or dry-run script.

6. Gate or allowlist Apple Shortcuts.
   - Effort: small-to-medium.
   - Payoff: prevents arbitrary local shortcut side effects.
   - Files: `config/tools.yaml`, destructive gating tests.

7. Add DCR caps/rate limit.
   - Effort: small.
   - Payoff: reduces public endpoint trash.
   - Files: `mcp_external/oauth.py`, OAuth tests.

### Accept For Now

These are valid concerns, but not worth a big layer today.

1. Plain local SQLite database.
   - Accept if laptop account is trusted and iCloud backup is scrubbed.

2. Full Keychain migration for every token.
   - Nice later. More important is preventing broad local-read tools from seeing `.env`.

3. Apple Reminders/Calendar ungated.
   - Accept if you prefer seamless mirroring and can tolerate occasional clutter.

4. OAuth token hashing at rest.
   - Do later if external MCP becomes important across devices. Scrub backups first.

5. Host-header hardening on OAuth parent app.
   - Nice defense-in-depth. Low urgency behind Cloudflare plus localhost bind.

## What I Would Change From The Original Severity Labels

The earlier report called many things "High" because it used standard review language. For this solo-dev deployment, I would recalibrate:

| Original | Solo-dev priority | Reason |
| --- | --- | --- |
| Truncated approval prompts | Fix now | This is the main safety boundary for dangerous tools. |
| Attachment delimiter escaping | Fix now | Tiny direct injection-boundary bug. |
| Unpinned MCP packages | Fix now | Cheap supply-chain risk reducer. |
| DuckDB ungated | Fix now or disable | Always-on broad local read is not worth it. |
| `python_run` broad reads | Fix soon after approval previews | It is already gated; full-code preview changes the risk a lot. |
| Apple Events ungated | Accept partly | Calendar/reminders are acceptable; Shortcuts deserve more caution. |
| Plain OAuth tokens in DB | Fix later | Local DB only is okay if raw backups are scrubbed. |
| iCloud DB backups | Fix soon | This is where "local only" stops being local. |
| Open DCR no caps | Fix later/small | Public nuisance more than compromise path. |
| Host validation on OAuth routes | Fix later | Defense-in-depth, not core. |
| Full secret migration | Fix later | Avoid local-read paths first. |

## Thin Implementation Plan

This is the smallest plan I would actually run.

1. Patch `telegram_bridge.py` to use `wrap_untrusted()` for inlined text and HTML attachments.
2. Add tests for forged untrusted delimiters in document ingest.
3. Replace approval summaries with a `render_approval_preview(tool_name, args)` helper:
   - exact recipients/destinations/paths/counts;
   - full `python_run` code or reject oversized code;
   - redacted secrets;
   - explicit `PREVIEW TRUNCATED - DO NOT APPROVE` when any critical field cannot fit.
4. Mark DuckDB as gated or remove it from the normal allowlist; add a test that it is not always-on.
5. Pin MCP package specs.
6. Scrub iCloud backups by copying to a temp DB, deleting auth/session-sensitive tables, vacuuming, then backing up the temp DB.
7. Gate Apple Shortcuts separately from Apple Events.

If you only want a two-hour security sprint, do steps 1-4.

## Existing Controls Worth Keeping

Do not rip these out; they are doing useful work without much friction.

- Telegram owner checks across handlers.
- Typed `CONFIRM-SEND` for high-impact tool calls.
- PostToolUse wrapping for untrusted external outputs.
- Canary leak filter on outbound approval text and normal replies.
- Path containment for attachment reader and wiki tools.
- PKCE and passphrase rate limiting in the external MCP OAuth flow.
- Read-only external MCP tool surface for recall/wiki search/open loops.

## Bottom Line

For a solo dev, the goal is not "maximum security." The goal is to make the dangerous paths honest and boring:

- you can see exactly what you approve;
- untrusted files cannot escape their wrapper;
- random package updates do not run with your tokens;
- broad local-read tools are not always-on;
- cloud backups do not carry live auth tokens.

That is enough security layer. Anything beyond that should earn its keep by removing a real annoyance or a real blast radius, not by looking impressive.
