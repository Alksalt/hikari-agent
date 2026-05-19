"""Phase 7: standalone MCP server that exposes Hikari's memory (read-only)
to remote callers — Claude Desktop, Claude iOS — via a Cloudflare Tunnel.

Architecture:

    Claude Desktop / iPhone (uses your Max subscription for its own inference)
                  ↓ MCP over Streamable HTTP
    https://hikari.<your-domain>.com    (Cloudflare Tunnel, TLS termination)
                  ↓ tunnel → 127.0.0.1:8765
    `uv run python -m mcp_external.launch`  (this package)
                  ↓ in-process Python imports
    same SQLite DB Hikari's Telegram bot uses

Bearer-token auth (``HIKARI_MCP_SECRET`` env var) on every request. Tools are
read-only — recall / lexicon_top / observations / open_loops / wiki_search.

Setup: see ``scripts/install_cloudflared.md``.
"""
