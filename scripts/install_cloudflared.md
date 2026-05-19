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
- Bearer token + TLS = good enough auth for a personal one-user bot.
  (OAuth 2.1 / PKCE is a future PR.)

## Setup (one-time, ~30 min)

### 1. Generate a bearer secret

```bash
openssl rand -hex 32
```

Copy the output into `.env` as `HIKARI_MCP_SECRET=...`.

### 2. Enable the server in config

In `config/engagement.yaml`, flip:

```yaml
mcp_external:
  enabled: true   # was false
  bind_host: "127.0.0.1"
  bind_port: 8765
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

### 8. Register the connector on claude.ai

- Open https://claude.ai/settings/integrations (or whichever path Anthropic
  uses in May 2026).
- "Add Custom Connector" → "Remote MCP".
- URL: `https://hikari.your-domain.com/mcp`
- Auth: Bearer token. Paste the value of `HIKARI_MCP_SECRET`.
- Click connect.

The 5 tools (`hikari_recall`, `hikari_lexicon_top`, `hikari_observations`,
`hikari_open_loops`, `hikari_wiki_search`) should appear in the tool list.

Once added on claude.ai, Claude Desktop and Claude iPhone pick up the
connector automatically (sync within minutes).

## Making both services restart-resilient (LaunchAgent)

Both `uv run python -m mcp_external.launch` and `cloudflared tunnel run` are
long-running processes that should restart on reboot/crash. The simplest
pattern is two separate LaunchAgents next to the existing `com.hikari.agent.plist`:

- `~/Library/LaunchAgents/com.hikari.mcp.plist` — runs the MCP server
- `~/Library/LaunchAgents/com.hikari.tunnel.plist` — runs cloudflared

Both should set `KeepAlive` and `RunAtLoad`. Use the same template style as
`scripts/install_launchd.sh` (the existing bot agent). One-time script TBD —
for now run them manually under tmux/screen until the workflow stabilizes.

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
