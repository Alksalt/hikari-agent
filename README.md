# hikari-agent

Greenfield rewrite of [hikari-tsukino-bot](../hikari-tsukino-bot/) on the Claude Agent SDK.
Single-user. Runs on Max subscription's $200/mo Agent SDK quota (no API key billing).

Phased plan: `/Users/alt/.claude/plans/memoized-fluttering-meerkat.md`.

## Status: Phases 1–10 complete

- **Agent loop:** Sonnet 4.6 primary, Haiku 4.5 fallback (`fallback_model`), session resume via SQLite.
- **Memory:** SQLite with `core_blocks`, bi-temporal `facts` (`valid_to` / `superseded_by`), `episodes`, `tasks`, `entities`, `character_thoughts`, `runtime_state`, FTS5 BM25. Park et al. retrieval scoring (recency × importance × relevance). `sqlite-vec` deferred (schema has `embedding BLOB` hooks ready).
- **Tools (in-process MCP):** `recall`, `remember`, `mark_fact_invalid`, `update_core_block`, `task_create`, `task_update`, `generate_photo`.
- **Skills:** `character-voice` (+ STAGES + LORE), `recall-memory`, `generate-photo`, `schedule-heartbeat` (+ EXAMPLES), `drive-search`.
- **Hooks:** `UserPromptSubmit` injects all `core_blocks` + open `tasks` + top-8 retrieved hits. `PostToolUseFailure` logs failures.
- **Background:** APScheduler — heartbeat every 30 min (Python gates conditions, Sonnet writes the message), session consolidation every 15 min, daily reflection at 09:00 local.
- **Photo gen:** OpenRouter Flux.2-klein via `@tool`; bridge drains `data/photo_outbox/` after each turn.
- **Migration:** `scripts/migrate_from_current.py` ports the old markdown layout to SQLite.

Google Workspace MCP (Phase 7) requires OAuth user credentials: set `GOOGLE_WORKSPACE_CLIENT_ID`, `GOOGLE_WORKSPACE_CLIENT_SECRET`, and `GOOGLE_WORKSPACE_REFRESH_TOKEN` in `.env`, then uncomment the server in `.mcp.json`.

### macOS native integrations

- **Apple Reminders + Calendar** (via the `apple_events` MCP server, EventKit): the first call triggers an Automation permission prompt for Reminders/Calendar — accept it in System Settings → Privacy & Security → Automation.
- **Apple Notes** (via in-process `note_create` / `note_search` / `note_read` tools, AppleScript): the first call to any of these triggers a macOS Automation permission prompt for Notes.app — accept it the same way. These tools are for quick capture / cross-device sticky notes; permanent personal knowledge lives in the Obsidian wiki.

## Setup

```bash
uv sync

# One-time: generate OAuth token tied to your Max subscription
claude setup-token
# Copy the printed token into .env as CLAUDE_CODE_OAUTH_TOKEN

cp .env.example .env
# Fill: CLAUDE_CODE_OAUTH_TOKEN, TELEGRAM_BOT_TOKEN, OWNER_TELEGRAM_ID
# (Optional for now: OPENROUTER_API_KEY for photos)

uv run hikari-agent
```

## Migrate from old bot (one-shot)

```bash
# Back up old data first
cp -r /Users/alt/work_dir/agents/hikari-tsukino-bot/data ~/hikari-data-backup

# Run migration (replace <owner_id> with your Telegram user ID)
uv run python scripts/migrate_from_current.py \
    /Users/alt/work_dir/agents/hikari-tsukino-bot/data/users/<owner_id>
```

## Layout

```
hikari-agent/
├── pyproject.toml
├── .env.example
├── .mcp.json                  # external MCP servers (Google Workspace, Notion, GitHub, etc.)
├── CLAUDE.md                  # always-loaded persona
├── AGENTS.md                  # delegation map, subagents, utility tools, runtime entrypoints
├── .claude/skills/
│   ├── character-voice/{SKILL.md, INTIMATE.md, LORE.md}
│   ├── recall-memory/SKILL.md
│   ├── generate-photo/SKILL.md
│   ├── schedule-heartbeat/{SKILL.md, EXAMPLES.md}
│   └── drive-search/SKILL.md
├── agents/
│   ├── runtime.py             # ClaudeSDKClient, 3-entrypoint split (run_user_turn /
│   │                          #   run_visible_proactive / run_internal_control), MCP wiring
│   ├── telegram_bridge.py     # Telegram polling, OWNER lock, _send_with_choreography (codex P0)
│   ├── hooks.py               # UserPromptSubmit memory injection, PreToolUse defer gate,
│   │                          #   PostToolUseFailure log, ExternalWrap untrusted-content hook
│   ├── proactive.py           # heartbeat, re-engagement, calendar heartbeat, Apple/GCal sync
│   ├── postsend.py            # observation/noticing surfaced-marking (post-send bookkeeping)
│   ├── reflection.py          # daily reflection, session consolidation, weekly consolidation
│   ├── reflection_sanitize.py # injection defense for reflection → core_blocks writes
│   ├── scheduler.py           # APScheduler job wiring
│   ├── subagents.py           # ALL_AGENTS: recall, wiki, code_dispatch, drive_gmail,
│   │                          #   notion, research, github
│   └── tool_inventory.py      # per-turn tool/subagent inventory block (anti-hallucination)
├── tools/
│   ├── apple_notes.py         # @tool note_create/search/read (AppleScript, macOS-only)
│   ├── attachments.py         # @tool read_attachment — scoped reader (Stream B)
│   ├── approvals.py           # defer-gate: PreToolUse hook + CONFIRM-SEND resume path
│   ├── memory.py              # @tool recall, remember, task_*, update_core_block, ...
│   └── photos.py              # @tool generate_photo (OpenRouter Flux)
├── storage/
│   ├── db.py                  # full schema + helpers
│   └── retrieval.py           # Park et al. scoring
├── config/
│   └── engagement.yaml        # all tunables: typing, proactive, approvals, defer gates, ...
├── scripts/
│   └── migrate_from_current.py
├── assets/
│   └── APPEARANCE.md
└── tests/                     # 561 collected; 543 pass, 18 skip, 0 fail
```

## Verify

```bash
uv run pytest -q   # 543+ passed, 18 skipped, 0 failed. All in-memory (no live API).
                   # Persona regression tests: uv run pytest tests/persona/ -q
uv run ruff check .
```

End-to-end: send any message to your Telegram bot, get a Sonnet reply in Hikari's voice. Check Anthropic console — should show 0 API spend, calls counted against Max quota.

## Risks / known compromises

- **Max SDK quota exhaustion** ($200/mo): at 200 msg/day × 30 days × ~3k tokens/turn ≈ 18M tokens. Mitigation: prompt caching on persona blocks (auto-enabled by SDK), `max_budget_usd=0.50` cap per turn, `max_turns=15` cap per turn.
- **Anthropic safety** may refuse explicit Stage 4–5 content from `STAGES.md`. If so, that content has to migrate back to an OpenRouter route via a separate `@tool` — TBD.
- **`sqlite-vec` deferred**: retrieval is BM25 + recency + importance only. Schema has `embedding BLOB` columns ready; adding cosine ranking is a follow-up.
