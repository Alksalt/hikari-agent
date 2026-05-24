# Cross-device sync: Hikari's memory in Claude Desktop + iPhone

This wires Hikari's memory tools (recall, lexicon, observations, open loops,
wiki search) as a Custom Connector on claude.ai. Once set up, you can ask
Hikari something from inside the Claude Desktop app or claude.ai mobile and
get her *real* memory back, not generic Claude.

## Why this works in May 2026

- Claude Desktop's `Settings → Integrations` accepts remote MCP servers
  speaking **Streamable HTTP** (SSE deprecated through 2026 but still works).
- Anthropic's broker calls the server **from Anthropic's cloud**, not from
  your device. So localhost is a non-starter — we need a public endpoint.
- A single Custom Connector registration on claude.ai syncs to Claude
  Desktop AND Claude iPhone automatically.
- **Cloudflare Tunnel** publishes a stable HTTPS URL without opening ports
  on your Mac Mini, terminating TLS at Cloudflare's edge.
- Two auth paths run side-by-side:
  - **Bearer service token** (`HIKARI_MCP_SECRET`) — used by Claude Code's
    `claude mcp add` and Claude Desktop's local config (where you can paste
    a static token).
  - **OAuth 2.1 + PKCE + Dynamic Client Registration** — required by
    claude.ai's web Custom Connector flow (iPhone, web UI). The broker
    auto-discovers via `/.well-known/oauth-protected-resource`, registers a
    client via `POST /register`, redirects you to `/authorize` where you
    enter the owner passphrase, then exchanges the code at `/token`.

## Setup (one-time, ~30 min)

### 1. Generate the auth secrets

```bash
# Bearer service-token (for Claude Code / Claude Desktop local config):
openssl rand -hex 32

# Owner passphrase (gates the OAuth /authorize consent form, claude.ai flow):
openssl rand -base64 24
```

Copy them into `.env` as `HIKARI_MCP_SECRET=...` and
`HIKARI_OAUTH_OWNER_PASSPHRASE=...`. You need at least one of the two for the
server to start. Need both if you want both auth paths working.

### 2. Enable the server in config

In `config/engagement.yaml`, flip + set `public_base_url` to your tunnel
hostname (from step 4 below — pre-fill it now to avoid a server restart
later). When this is empty, OAuth discovery responses embed the inbound
hostname (`127.0.0.1:8765`) which claude.ai's broker cannot reach, and the
OAuth add-connector flow breaks silently with no diagnostic from our side.

```yaml
mcp_external:
  enabled: true   # was false
  bind_host: "127.0.0.1"
  bind_port: 8765
  public_base_url: "https://hikari.your-domain.com"   # required for OAuth flow
  behind_tls_proxy: true   # Cloudflare terminates TLS; mark cookies Secure
```

### 3. Install + auth Cloudflare Tunnel

```bash
brew install cloudflared
cloudflared tunnel login   # opens a browser; choose your Cloudflare account
```

You need a domain managed by Cloudflare DNS. If you don't have one, point
a (free) `.workers.dev` subdomain or grab a cheap domain — the rest of the
flow assumes `hikari.your-domain.com` is available.

### 4. Create the tunnel

```bash
cloudflared tunnel create hikari-mcp
# Note the tunnel UUID from the output.

# Route DNS:
cloudflared tunnel route dns hikari-mcp hikari.your-domain.com
```

### 5. Tunnel config

Create `~/.cloudflared/config.yml`:

```yaml
tunnel: <tunnel-UUID-from-step-4>
credentials-file: /Users/<you>/.cloudflared/<tunnel-UUID>.json

ingress:
  - hostname: hikari.your-domain.com
    service: http://127.0.0.1:8765
  - service: http_status:404
```

### 6. Run the MCP server

In a terminal (or under LaunchAgent — see below):

```bash
cd ~/work_dir/agents/hikari-agent
uv run python -m mcp_external.launch
```

It refuses to start if `HIKARI_MCP_SECRET` is empty or `mcp_external.enabled`
is false — both must be set.

### 7. Run the tunnel

In another terminal:

```bash
cloudflared tunnel run hikari-mcp
```

Smoke-test from any machine:

```bash
curl -H "Authorization: Bearer $HIKARI_MCP_SECRET" \
  https://hikari.your-domain.com/mcp
# (should not return 401; MCP returns JSON-RPC.)
```

### 8. Register the connector on claude.ai (OAuth path)

(`mcp_external.public_base_url` was set in step 2; the metadata responses
already point at the right hostname.) On claude.ai:

- Open https://claude.ai/settings/integrations.
- "Add Custom Connector" → "Remote MCP".
- URL: `https://hikari.your-domain.com` (root, NOT `/mcp` — the broker hits
  `/.well-known/oauth-protected-resource` for auto-discovery).
- The broker walks through OAuth automatically:
  1. Discovers `authorization_endpoint` + `token_endpoint`.
  2. POSTs `/register` for a fresh `client_id`.
  3. Redirects your browser to `/authorize?...`.
  4. The hikari passphrase page renders — paste
     `HIKARI_OAUTH_OWNER_PASSPHRASE`.
  5. Server 302s back with `code=...`; the broker exchanges it at `/token`.
- The 5 tools (`hikari_recall`, `hikari_lexicon_top`, `hikari_observations`,
  `hikari_open_loops`, `hikari_wiki_search`) should appear once consented.

Once added on claude.ai, Claude Desktop and Claude iPhone pick up the
connector automatically (sync within minutes).

### 8b. Alternative: Claude Code / Desktop local config (bearer path)

For local-config-driven clients that accept a static token, skip the OAuth
dance and use the bearer path:

```bash
# Claude Code:
claude mcp add hikari https://hikari.your-domain.com \
  --transport http \
  --header "Authorization: Bearer $HIKARI_MCP_SECRET"
```

Or paste the same `Authorization: Bearer ...` header in Claude Desktop's
local config file. Both paths talk to the same five tools and the same
audit ledger (different `approved_by` attribution — `oauth:<client_id>` for
OAuth, `external_mcp` for bearer).

## Making both services restart-resilient (LaunchAgent)

Both `uv run python -m mcp_external.launch` and `cloudflared tunnel run` are
long-running processes that should restart on reboot/crash. Install both as
LaunchAgents next to the existing `com.hikari.agent.plist`:

```bash
# Install the external MCP server (com.hikari.mcp):
bash scripts/install_external_mcp_launchd.sh

# Install the cloudflared tunnel (com.hikari.tunnel):
bash scripts/install_cloudflared_launchd.sh
```

Both scripts are idempotent — re-running is safe. They write plists to
`~/Library/LaunchAgents/` and `launchctl bootstrap` them immediately.

| Label                  | Purpose                                                  |
|------------------------|----------------------------------------------------------|
| `com.hikari.mcp`       | runs `uv run python -m mcp_external.launch`              |
| `com.hikari.tunnel`    | runs `cloudflared tunnel run hikari-mcp`                 |

Common ops:

```bash
# status
launchctl print gui/$(id -u)/com.hikari.mcp
launchctl print gui/$(id -u)/com.hikari.tunnel

# restart
launchctl kickstart -k gui/$(id -u)/com.hikari.mcp
launchctl kickstart -k gui/$(id -u)/com.hikari.tunnel

# uninstall
bash scripts/install_external_mcp_launchd.sh --uninstall
bash scripts/install_cloudflared_launchd.sh --uninstall
```

## Security notes

- Bearer secret leaks → rotate via `openssl rand -hex 32` → update `.env`
  and the claude.ai connector → restart the server.
- Tunnel URL is sensitive — Cloudflare Tunnel logs at `cloudflared.log`
  show every request; rate-limit via Cloudflare Access policies if needed.
- Server tools are READ-ONLY. No writes from Claude Desktop / iPhone.
- All outputs are wrapped via `agents.injection_guard.wrap_untrusted`
  so the remote caller's LLM treats them as data, not instructions.
- Every tool call is audit-logged with `external_mcp:<tool>` prefix in
  the `audit_log` table.

## Troubleshooting

- **401 on every request**: `HIKARI_MCP_SECRET` is empty in the env the
  server is reading. Confirm via:
  `cloudflared tunnel run` logs vs `uv run python -m mcp_external.launch` logs.
- **Tunnel up, MCP not responding**: check `127.0.0.1:8765` directly
  (`curl http://127.0.0.1:8765/mcp` from the Mac Mini) — if that's also dead,
  the MCP server isn't running.
- **claude.ai says "tools available: 0"**: connector connected but no
  tools enumerated → likely the bearer fails the initial discovery handshake.
  Recopy the token.
