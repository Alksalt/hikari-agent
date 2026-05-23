# Security Review - Hikari Agent

Date: 2026-05-23

Scope: Python Telegram agent, external MCP/OAuth server, tool registry, prompt-injection boundaries, local execution tools, secret handling, and backup flows in `/Users/ol/agents/hikari-agent`.

Guidance used: Python ASGI/FastAPI/Starlette secure-baseline guidance from the `security-best-practices` skill, plus project tests/docs/code review.

## Executive Summary

No confirmed remote unauthenticated write path was found. The external MCP server has several strong controls: it refuses to start without a bearer secret or OAuth passphrase, uses constant-time bearer comparison, uses PKCE for OAuth codes, rate-limits the owner passphrase step, wraps read-only external tool output, and exposes only read-only Hikari tools externally.

The main risks are privilege-boundary issues around tools that can read local files, execute local automation, or use high-value third-party tokens. Several approval and wrapping layers exist, but a few paths still let dangerous details be hidden, bypass delimiter escaping, or rely on informal "read-only by contract" behavior.

Highest-priority fixes:

1. Replace truncated approval summaries with full, structured previews for privileged calls.
2. Gate or sandbox the DuckDB, Apple Events, Apple Shortcuts, and `python_run` surfaces more tightly.
3. Pin all external MCP package versions and remove `@latest`/floating `npx -y`/`uvx --from` installs from runtime startup.
4. Hash external MCP OAuth tokens at rest and encrypt or scrub database backups.
5. Route uploaded text/HTML attachment inlining through `wrap_untrusted()`.

## High Severity

### H-01: Privileged approval prompts truncate executable or destination-critical arguments

Locations:

- `agents/hooks.py:620-627`
- `agents/hooks.py:763-790`
- `tools/gatekeeper_can_use_tool.py:59-66`

Evidence:

`_summary_for_defer()` serializes the tool input to JSON and truncates it at 240 characters before showing it to the owner. The older gatekeeper path truncates at 200 characters. These summaries are the human decision surface for gated tools such as Gmail send, Google Drive upload/delete, Notion writes, GitHub writes, and `python_run`.

Impact:

An attacker-controlled prompt or compromised tool output can place the benign-looking part first and hide the dangerous recipient, code tail, file path, destructive flag, or message body after the truncation point. The stored deferred args remain complete, so approval can execute something the owner never actually saw.

Fix:

Use per-tool structured previews instead of raw truncated JSON. For example: show email recipients, subject, attachment count, body length, and a full body preview link; show `python_run` code in full or refuse if it exceeds preview length; show Drive/Notion/GitHub destination, operation, object IDs, and mutation counts. If content is too large for Telegram, write a temporary local preview artifact and include a digest plus a clear "full preview required" step before approval.

### H-02: DuckDB MCP is an ungated local SQL engine with broad file-read potential

Locations:

- `config/tools.yaml:115-129`
- `config/tools.yaml:610-616`
- `docs/duckdb_mcp.md:10-17`
- `docs/duckdb_mcp.md:38-42`
- `docs/duckdb_mcp.md:99-104`
- `tests/test_duckdb_mcp.py:38-42`

Evidence:

The DuckDB MCP server starts from `uvx --from mcp-server-motherduck` with an in-memory database. The tool registry exposes `mcp__duckdb__*` with `gate: null`. The documentation says user data is protected by attaching specific SQLite files with `READ_ONLY`, and that use is "read-only by contract."

Impact:

`READ_ONLY` protects attached SQLite stores from SQL mutation, but it does not make the DuckDB process a file-system sandbox. A general DuckDB query engine can read local files through file-reading table functions, globbing, extension loading, or future package behavior, depending on server capabilities. Because this wildcard is always allowed, prompt injection from any untrusted content can attempt local-file reads from `.env`, `data/hikari.db`, iCloud wiki files, or other user files.

Fix:

Replace the generic MCP with a narrow in-process analytics tool that accepts a fixed query template or limited query grammar. If keeping DuckDB, run it under a macOS sandbox profile or container with only the intended SQLite files readable, pre-load required extensions, disable runtime extension install, and gate all ad hoc SQL with full query preview.

### H-03: `python_run` sandbox blocks network and writes, but permits broad local reads

Locations:

- `config/tools.yaml:414-419`
- `tools/calc/python_run.py:57-74`
- `tools/calc/_shared.py:122-184`

Evidence:

`python_run` is defer-gated and executed with `sandbox-exec`, isolated Python, no network, no fork/exec, and tmpdir-only writes. However, the sandbox profile starts with `(allow default)` and only selectively denies reads under paths such as `~/.ssh`, `~/.aws`, `~/.config`, and selected `/etc` files.

Impact:

After approval, code can still read most local files visible to the user, including repo secrets, `.env`, `secrets/`, SQLite databases, and the Obsidian vault. Combined with H-01, a long code snippet can hide a local-file read or data dump after the truncated preview.

Fix:

Change the sandbox to deny `file-read*` by default and explicitly allow only Python runtime paths, the temporary directory, and user-supplied input files. If that is too brittle for macOS Python startup, run this in an ephemeral container/VM-style environment with no secret mounts. Require full-code preview before approval.

### H-04: Runtime MCP dependencies are unpinned and execute with high-value credentials

Locations:

- `config/tools.yaml:50-62`
- `config/tools.yaml:64-80`
- `config/tools.yaml:82-95`
- `config/tools.yaml:98-104`
- `config/tools.yaml:115-129`

Evidence:

Several bucket-3 MCP servers are launched directly from floating package specs: `uvx --from google-workspace-mcp`, `npx -y @notionhq/notion-mcp-server`, `npx -y @modelcontextprotocol/server-github`, `npx -y @playwright/mcp@latest`, `npx -y mcp-server-apple-events`, `npx -y mcp-server-apple-shortcuts`, and `uvx --from mcp-server-motherduck`.

Impact:

Those processes run as the local user. Some receive Google refresh tokens, Notion tokens, GitHub PATs, browser automation access, or local Apple automation access. A compromised package release, dependency confusion event, or unexpected breaking update can become local code execution with those credentials.

Fix:

Pin exact versions or immutable Git SHAs for every external MCP. Prefer checked-in lockfiles or vendored wrappers over network package resolution during normal startup. Run each server with the minimum environment variables it needs. Consider a periodic, manual dependency-update workflow with review, test, and rollback.

### H-05: Uploaded text/HTML attachment inlining bypasses delimiter escaping

Locations:

- `agents/telegram_bridge.py:986-991`
- `agents/telegram_bridge.py:1007-1012`
- `agents/injection_guard.py:92-138`
- `tests/test_security.py:55-71`

Evidence:

`wrap_untrusted()` escapes forged `<<<HIKARI_UNTRUSTED_BEGIN>>>` and `<<<HIKARI_UNTRUSTED_END>>>` strings before wrapping. However, uploaded HTML and text files are manually wrapped in `telegram_bridge.py` with raw delimiter strings and no delimiter escaping.

Impact:

A malicious uploaded `.txt`, `.md`, `.html`, `.json`, or code file can contain the close delimiter, escape the data block, and put follow-on instructions outside the apparent untrusted region. That weakens the project's prompt-injection boundary specifically for user-forwarded files.

Fix:

Use `injection_guard.wrap_untrusted("telegram_document", text)` for text and stripped-HTML inlining instead of hand-built delimiters. Add regression tests that upload content containing both forged delimiters and assert only the final real delimiter remains unescaped.

### H-06: Apple Events and Apple Shortcuts wildcard tools are ungated

Locations:

- `config/tools.yaml:578-592`
- `tests/test_destructive_tool_gating.py:158-186`
- `tests/test_destructive_tool_gating.py:188-219`

Evidence:

The registry exposes `mcp__apple_events__*` and `mcp__apple_shortcuts__*` with `gate: null`. Tests explicitly assert that Apple Events write tools, including reminder/calendar create/delete operations, do not trigger the defer hook.

Impact:

The comment classifies these as low-risk local-device actions, but they can still create/delete calendar events and reminders, and Shortcuts may perform arbitrary local workflows depending on installed shortcuts. A prompt-injection chain from email, web, docs, or attachments could cause local OS side effects without owner confirmation.

Fix:

Gate state-changing Apple Events and Apple Shortcuts tools. If reminder mirroring needs an internal fast path, split it from user-facing MCP tools by using a contextvar, a private in-process function, or a narrower confirmed wrapper rather than exposing the wildcard ungated.

### H-07: External MCP OAuth tokens are stored as plaintext bearer tokens

Locations:

- `storage/db.py:407-421`
- `storage/db.py:3113-3129`
- `storage/db.py:3132-3147`

Evidence:

`oauth_tokens.token` is the primary key, and minted access/refresh token strings are inserted directly into SQLite. Validation queries by the raw token and returns the row.

Impact:

Anyone who obtains `data/hikari.db` or a backup can use active access tokens immediately and refresh tokens until their expiry or revocation. Because the external MCP server exposes personal memory, observations, open loops, and wiki search, token theft is a privacy breach even though the external tools are read-only.

Fix:

Store only a keyed digest, such as `HMAC-SHA256(server_secret, token)`, plus a token prefix for debugging. Return the raw token only once at mint time. Migrate by revoking all existing OAuth tokens and forcing connector re-authorization.

## Medium Severity

### M-01: Full SQLite backups are copied to iCloud without encryption or token scrubbing

Locations:

- `scripts/backup.sh:13-16`
- `scripts/backup.sh:23-25`
- `scripts/backup.sh:41-49`

Evidence:

The backup script copies `data/hikari.db` into the Obsidian iCloud vault and keeps recent `.db` files. There is no encryption step, permission hardening, or token/data scrubbing.

Impact:

Backups can contain chat history, memory, private facts, approval rows, OAuth token rows, and possibly other sensitive agent state. Syncing full plaintext databases expands the blast radius to iCloud, Obsidian plugins, other devices, and local vault backups.

Fix:

Encrypt backups before writing to the vault, for example with `age` or a macOS Keychain-backed key. Alternatively back up a scrubbed export that excludes `oauth_tokens`, approval arguments, and high-risk logs. Set restrictive file permissions and document restore/rotation steps.

### M-02: Dynamic client registration has no rate limit, request-size cap, or field-length caps

Locations:

- `mcp_external/oauth.py:227-260`
- `storage/db.py:3010-3026`

Evidence:

`register_client()` is intentionally open DCR. It validates that `redirect_uris` is a non-empty list of HTTP(S) URLs without embedded credentials, then inserts the client metadata. There is no per-IP throttle, total body cap, max redirect URI count, URI length cap, or client name length cap visible in this path.

Impact:

A public deployment can be spammed with large or numerous client registrations, growing `oauth_clients` and `oauth_audit_log` and consuming disk/CPU. This is not an auth bypass by itself, but it is a public unauthenticated write endpoint.

Fix:

Add per-IP rate limiting to `/register`, cap body size at the ASGI/proxy layer, cap redirect URI count and lengths, cap `client_name`, and periodically prune unused clients. Consider requiring an owner-issued registration secret if open DCR is not strictly needed.

### M-03: OAuth discovery and authorization routes do not have app-level host validation

Locations:

- `mcp_external/server.py:134-155`
- `mcp_external/launch.py:100-104`
- `mcp_external/launch.py:262-266`

Evidence:

FastMCP is configured with DNS rebinding protection and allowed hosts/origins. The parent Starlette app mounts unauthenticated OAuth/discovery routes before the FastMCP app, and `AuthMiddleware` passes OAuth prefixes through without authentication. No explicit `TrustedHostMiddleware` or equivalent host check is applied to the parent OAuth routes.

Impact:

If the app is ever exposed outside the expected reverse proxy shape, OAuth discovery/authorize/token behavior can be reached with arbitrary Host headers. Existing config may reduce practical impact, but host validation should live in the ASGI app as a defense-in-depth control.

Fix:

Apply Starlette `TrustedHostMiddleware` or an equivalent custom check to the parent app using the same `public_base_url`/localhost allowlist as FastMCP. Keep proxy host validation too.

### M-04: Approval prompts can leak sensitive argument values into Telegram

Locations:

- `agents/hooks.py:620-627`
- `agents/hooks.py:763-790`
- `tools/approvals.py:490-504`

Evidence:

The defer prompt summary uses raw JSON from `tool_input`. A redaction helper exists in `tools/approvals.py`, but this prompt path does not use it before sending args over Telegram.

Impact:

If a gated tool call includes secrets, auth headers, private document text, or token-bearing URLs, those values can be copied into Telegram approval messages. This may be acceptable for some body previews, but secrets should be redacted by default.

Fix:

Run summaries through the existing redaction helper, then add per-tool allowlisted preview fields. For message bodies, show content intentionally; for credentials, URLs with tokens, headers, cookies, or env vars, redact.

### M-05: Secret migration is incomplete and Keychain is optional by default

Locations:

- `.env.example:37-43`
- `.env.example:54-62`
- `.env.example:124-126`
- `scripts/migrate_secrets_to_keychain.py:29-43`
- `auth/google.py:61-76`
- `auth/store.py:90-112`

Evidence:

`.env.example` includes Google client secret and refresh token, Notion token, GitHub PAT, `HIKARI_MCP_SECRET`, and OAuth owner passphrase. The migration script only moves a smaller daemon-critical set and explicitly skips Notion. The auth store falls back to an in-memory store unless `HIKARI_REQUIRE_KEYCHAIN=1`.

Impact:

Long-lived cloud tokens can remain in `.env` by default. That increases risk from local file reads, DuckDB/Python execution, backups, accidental editor exposure, and future logs.

Fix:

Expand the migration script to cover Google Workspace credentials, GitHub PAT, Notion token, external MCP bearer secret, OAuth passphrase, and other long-lived production secrets. Set `HIKARI_REQUIRE_KEYCHAIN=1` in production launch configs and document a one-command rotation path.

### M-06: Scope precheck defaults to observe-only mode

Locations:

- `agents/hooks.py:630-680`

Evidence:

`AUTH_PRECHECK` defaults to `shadow`. Missing scopes are logged but do not deny tool execution unless the environment is set to `enforce`.

Impact:

This is a safe rollout pattern, but it means production can drift into using overbroad, stale, or mis-scoped credentials without a hard stop. It also makes tests pass even when scope intent and actual credential scope diverge.

Fix:

Set `AUTH_PRECHECK=enforce` for production once the current scope map is stable. Keep a CI test that exercises representative Google/Notion/GitHub tool scope checks.

## Low Severity

### L-01: Google OAuth setup script prints long-lived secrets to terminal

Locations:

- `scripts/setup_google_oauth.py:93-100`

Evidence:

The setup script prints the Google client secret and refresh token with instructions to paste them into `.env`.

Impact:

Terminal scrollback, shell session capture, or screen recording can expose the refresh token. This is mostly a setup-time operator hygiene risk.

Fix:

Write directly to Keychain or to a `0600` temp file by default. Make printing secrets an explicit `--print-env` mode with a warning.

### L-02: `calc` timeout can leak one worker thread per runaway expression

Location:

- `tools/calc/_shared.py:110-119`

Evidence:

The evaluator shuts down its thread pool with `wait=False` after timeout, so runaway eval code can leave a worker thread behind.

Impact:

Repeated injected or accidental runaway expressions can degrade the process over time. This is lower impact than `python_run`, but `calc` is usually available without approval.

Fix:

Move all expression evaluation to a subprocess with a hard kill timeout, or recycle a bounded worker process pool. Add rate limiting for repeated timeouts.

## Positive Controls Observed

- The Telegram bridge consistently checks owner identity before handling text, photos, voice, location, documents, and commands.
- Attachment reads are path-contained to approved upload roots and size-capped.
- Wiki path resolution uses containment checks before reading/writing vault files.
- External MCP server refuses unauthenticated startup and compares bearer tokens with `secrets.compare_digest()`.
- External MCP tools are read-only and wrap returned private-memory/wiki text before returning it.
- OAuth authorization codes use PKCE S256, single-use consume semantics, and owner-passphrase rate limiting.
- Existing prompt-injection tests cover delimiter escaping, canary tripwire behavior, and external tool output wrapping.
- Gated Google Workspace, Notion, and GitHub destructive tools have regression tests showing the defer hook still fires.

## Recommended Fix Order

1. Fix approval previews and redaction first, because this affects every gated high-privilege tool.
2. Gate or sandbox DuckDB, Apple Events/Shortcuts, and `python_run` before relying on prompt-injection defenses.
3. Pin external MCP dependencies and remove floating runtime installs.
4. Fix attachment wrapping through `wrap_untrusted()`.
5. Hash external MCP OAuth tokens and rotate existing tokens.
6. Encrypt or scrub database backups.
7. Add DCR throttles/caps and app-level host validation.
8. Expand Keychain migration and enforce scope prechecks in production.

## Suggested Regression Tests

- A defer approval test with a long Gmail/Drive/Python payload where the dangerous tail must be visible or the approval must be refused.
- A Telegram document ingest test containing forged `<<<HIKARI_UNTRUSTED_END>>>` and `<<<HIKARI_UNTRUSTED_BEGIN>>>` delimiters.
- A `python_run` sandbox test proving `.env`, `data/hikari.db`, and the wiki vault cannot be read.
- A DuckDB test or integration harness proving only approved database paths can be read.
- A registry test asserting every bucket-3 external package spec is pinned.
- OAuth storage tests proving only token digests are persisted after migration.
