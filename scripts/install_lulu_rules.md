# LuLu egress allowlist for hikari-agent

LuLu (https://objective-see.org/products/lulu.html) is a free, open-source
macOS firewall that alerts on outbound connections. Recommended for any
long-running personal-AI daemon that has credentials for your accounts.

## Why

Hikari has tokens for Telegram, Anthropic, OpenRouter, Google, eventually
Notion + Tavily. If anything inside the process tried to phone home to a
random IP (a compromised dep, a misbehaving MCP server, a model emitting
garbage URLs that a tool then fetched), you want to know.

## Install

1. Download LuLu from https://objective-see.org/products/lulu.html
2. Run the .pkg installer. Grant the System Extension permission in
   System Settings → Privacy & Security.
3. Open LuLu Preferences → Mode: **alert** (not silent-allow).

## Rules to add (allow)

When Hikari starts for the first time, LuLu will prompt for each connection.
Allow these explicitly:

| Process                        | Destination                  | Notes                                    |
| ------------------------------ | ---------------------------- | ---------------------------------------- |
| Python (`uv run hikari-agent`) | `api.telegram.org:443`       | bot polling                              |
| Python                         | `api.anthropic.com:443`      | Claude Agent SDK + dispatched sessions   |
| Python                         | `openrouter.ai:443`          | Flux photo gen                           |
| Python                         | `*.googleapis.com:443`       | Drive / Gmail / Calendar (Phase 4)       |
| Python                         | `api.tavily.com:443`         | research subagent (Phase 4)              |
| Python                         | `huggingface.co:443`         | one-time fastembed model download        |
| Python                         | `cdn-lfs*.huggingface.co:443`| ditto                                    |

Block everything else by default — LuLu will let you know if a new connection
attempt happens, which is exactly when you want to investigate.

## Rules to add (block)

Nothing explicit needed — the default-deny behavior handles it. But if you
ever want to specifically deny something noisy:

- Telemetry endpoints from any dep (e.g. PostHog, Sentry, Mixpanel).
- Update checkers that don't need to phone home.

## Audit + rotation

Every 90 days, review the LuLu allowlist for any "Python" rules you don't
recognize. If anything looks weird, kill the bot, investigate, then either
remove the rule or rotate the relevant token via the original provider.
