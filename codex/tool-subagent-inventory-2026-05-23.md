# Tool and Subagent Inventory Review - 2026-05-23

## Scope

This report inventories the Hikari runtime tool surface: in-process MCP tools,
external MCP connectors, specialist subagents, internal bridge helpers,
operator scripts, registry/approval infrastructure, and major scheduled
workflows that call tools indirectly.

Sources reviewed:

- `config/tools.yaml`
- `.mcp.json`
- `agents/runtime.py`
- `agents/hooks.py`
- `agents/tool_inventory.py`
- `agents/subagents/prompts/*`
- `tools/**`
- `mcp_external/**`
- `scripts/**`
- relevant tests around allowlists, gating, wrapping, scopes, and registry drift

Four parallel read-only explorers were also used for local tools, external
integrations/subagents, operational scripts, and security/gating coverage.

## Executive Summary

The architecture is now much cleaner than the old hand-maintained allowlist:
`config/tools.yaml` is the central registry for MCP servers, explicit tool
metadata, wrapping rules, approval gates, and subagent definitions. Utility
tools are auto-discovered from `tools/**/ALL_TOOLS`, and bucket-1 servers are
created in-process through `agents/runtime.py`.

The main operational risk is not missing access. It is too much future access:
external wildcard grants such as `mcp__google_workspace__*`, `mcp__notion__*`,
and `mcp__github__*` allow new upstream write tools to enter the runtime unless
they are explicitly classified and gated. Google Workspace is the sharpest case
because the specialist prompt advertises many Docs, Sheets, Slides, Drive, and
Gmail write tools, while only a subset are explicitly gated.

The second systemic risk is policy drift: prompts, tests, and registry metadata
can disagree. Example: the Google specialist prompt still says calendar adds and
Drive uploads auto-run, but the registry gates both. Runtime wins, but prompt
drift makes future audits harder.

## Runtime Model

### Tool Registry

- `config/tools.yaml` defines:
  - bucket-1 in-process MCP servers
  - bucket-3 external MCP servers
  - explicit tools and wildcard grants
  - `gate: null | defer | gatekeeper`
  - untrusted output wrapping patterns
  - subagent definitions
- `tools/_tools_yaml.py` parses the registry and exposes query methods for:
  - allowed tool names
  - defer-gated regexes
  - wrap patterns
  - untrusted-output source list
  - subagent `AgentDefinition`s
  - server specs
- `agents/runtime.py` builds `ClaudeAgentOptions` from the registry.
- `tools/_registry.py` auto-discovers utility tools under `tools/`.
- `scripts/regen_mcp_json.py` projects bucket-3 servers into `.mcp.json`.
- `scripts/validate_tool_registry.py` checks registry and `.mcp.json` drift.

### Approval and Safety Layers

- Runtime runs with `permission_mode="acceptEdits"`. The project gates, not the
  Claude Code UI allowlist, are the real guardrails.
- `gate: defer` uses `agents/hooks.py:defer_gated_tools`:
  - halts the SDK tool call
  - creates an approval row
  - sends a Telegram prompt
  - resumes only after exact `CONFIRM-SEND`
- `gate: gatekeeper` uses `tools/gatekeeper_can_use_tool.py`:
  - blocks inside SDK `can_use_tool`
  - awaits owner approval or timeout
  - currently used for Google Gmail bulk delete
- `agents/external_wrap_hook.py` wraps configured untrusted tool outputs after
  tool use.
- `agents/injection_guard.py` supplies delimiter wrapping and canary detection.
- `config/scopes.yaml` maps known destructive external tools to provider scopes.
  The runtime precheck defaults to `AUTH_PRECHECK=shadow`, so missing scopes warn
  but do not block unless production sets `AUTH_PRECHECK=enforce`.

## Subagents

| Subagent | Model | Tools | Purpose | Notes |
|---|---:|---|---|---|
| `wiki` | Haiku | `mcp__hikari_wiki__*` | Search/read/list/append/backlink the Obsidian vault | Append is ungated but audit logged. Heavy curation still needs care around vault conventions. |
| `drive_gmail` | Haiku | `mcp__google_workspace__*` | Gmail, Calendar, Drive, Docs, Sheets, Slides | Highest-risk wildcard. Some writes are gated, many upstream writes appear wildcard-auto-allowed. |
| `notion` | Haiku | `mcp__notion__*` | Search/query Notion databases, create/update pages/blocks | Prompt requires schema introspection. Write gates cover main page/block writes. |
| `research` | Sonnet | `WebSearch`, `WebFetch`, `mcp__playwright__*` | Fresh web research and JS-rendered fallback | Web/browser outputs are wrapped. Playwright package is `@latest`. |
| `github` | Haiku | `mcp__github__*` | Issues, PRs, repos, commits, releases | Gated subset covers create/merge/delete. Other state-changing upstream tools may auto-run via wildcard. |

Deleted/merged legacy subagents:

- `recall` is now direct tool `mcp__hikari_memory__recall`.
- `code_dispatch` is now direct tool `mcp__hikari_dispatch__dispatch_claude_session`.

## In-Process Dedicated MCP Servers

### `hikari_memory`

| Tool | Purpose | Gate | Risks / gaps |
|---|---|---|---|
| `recall` | Search private facts/episodes/tasks via retrieval | None | Output is untrusted-wrapped. Confidence calibration reduces fabrication risk. |
| `remember` | Store atomic facts, optionally superseding prior facts | None | Powerful memory write. Embedding failures are swallowed. |
| `mark_fact_invalid` | Invalidate stored fact by id | None | Ungated memory mutation. |
| `update_core_block` | Overwrite always-on memory blocks | None | Very powerful context mutation; ungated. |
| `task_create` | Track fuzzy open loops | None | Low risk, but can create noisy injected context. |
| `task_update` | Complete/drop/update open loops | None | Ungated ledger mutation. |

### `hikari_wiki`

| Tool | Purpose | Gate | Risks / gaps |
|---|---|---|---|
| `wiki_search` | Fuzzy filename/full-text vault search | None | Output wrapped as untrusted. |
| `wiki_read` | Read one note with frontmatter/body | None | Manual wrapping plus PostToolUse wrapping can double-wrap. |
| `wiki_append` | Append content to note/H2 | None | Audit logged, but does not enforce `updated:`, `index.md`, or `log.md` conventions. |
| `wiki_backlinks` | Follow `[[wikilink]]` graph | None | Read-only. |
| `wiki_list` | One-level fresh folder listing | None | Path contained under vault. |
| `wiki_tree` | Depth-limited recursive tree | None | Path contained under vault; output wrapped. |

### `hikari_photo`

| Tool | Purpose | Gate | Risks / gaps |
|---|---|---|---|
| `generate_photo` | Generate a Hikari photo and queue it in `data/photo_outbox` | Mood gate + daily cap | Uses OpenRouter Flux with mostly static prompt. No approval, writes local image files. |

Internal helper:

- `classify_photo_intent` is bridge-only, not MCP-exposed. It sends inbound
  user images to Anthropic Messages API using `CLAUDE_CODE_OAUTH_TOKEN` or
  `ANTHROPIC_API_KEY`, parses strict YAML, sanitizes details, and safely falls
  back to `other`.

### `hikari_dispatch`

| Tool | Purpose | Gate | Risks / gaps |
|---|---|---|---|
| `dispatch_claude_session` | Spawn autonomous background Claude Code worker in another repo | `defer` only when requested `allowed_tools` contains `Edit`, `Write`, or `Bash` | Free-form allowed tool strings could miss future powerful tool names. |
| `dispatch_claude_session_confirmed` | Approval-resume sibling | Conditional server only | Not normally allowlisted. |

### `hikari_codex`

| Tool | Purpose | Gate | Risks / gaps |
|---|---|---|---|
| `list_codex_reports` | List `.md` reports under `codex/` | None | Read-only. |
| `read_codex_report` | Read one report with 200 KB cap | None | Manual wrapping plus PostToolUse wrapping can double-wrap. |

## Utility Tools

Utility tools are hosted on `hikari_utility` and auto-discovered from
`tools/**/ALL_TOOLS`.

| Tool group | Tools | Purpose | Gate | Risks / gaps |
|---|---|---|---|---|
| Reminders | `reminder_create`, `reminder_list`, `reminder_cancel`, `reminder_snooze` | Telegram push reminders, optionally mirrored to Google Calendar and Apple Reminders | None | `reminder_create` defaults to GCal sync and Apple sync on macOS, so a local tool call can indirectly create calendar/reminder artifacts ungated. |
| Apple Notes | `note_create`, `note_search`, `note_read` | Quick capture/search/read through `osascript` | None | `note_create` is an ungated user-visible write. AppleScript quoting and timeout are covered. |
| Attachments | `read_attachment` | Read uploaded user files under `data/user_photos` or `data/user_documents` | Path containment + 8 MiB cap | Base64 image output can be large. Correctly marked untrusted. |
| Calc | `calc`, `python_run` | Safe expression eval; sandboxed Python snippets | `python_run` is `defer`; `calc` none | `python_run` depends on macOS `sandbox-exec`; `calc` timeout can leak worker threads by design. |
| Link Shelf | `link_save`, `link_search`, `link_list`, `link_update`, `link_delete` | Save/search/manage URL shelf | None | `link_save` fetches arbitrary http(s) with redirects and no private-IP block. Link output is not explicitly registered in YAML for untrusted wrapping. |
| Day Receipt | `receipt_add`, `receipt_today`, `receipt_get`, `receipt_print`, `receipt_week`, `receipt_search`, `receipt_set_note`, `receipt_delete` | Local Made/Moved/Learned/Avoided log | None | `receipt_delete` is permanent and ungated; no audit/soft delete. |
| Decision Log | `decision_log_capture`, `decision_log_resolve` | Calibration ledger | None | `resolve_by` validation is loose; local ledger writes are ungated. |
| Translation | `translate` | DeepL/LibreTranslate plus Japanese romaji | None | Sends text to third-party translation backend. Output wrapped. |
| Weather | `weather_fetch` | Open-Meteo + met.no consensus forecast | None | Read-only public API. |
| Currency | `currency_convert` | Frankfurter ECB daily conversion | None | Read-only public API. |
| arXiv | `arxiv_search` | Recent ML/DL paper search | None | Read-only public API. |
| Places | `places_search`, `place_open_now` | OSM Overpass POI/open-hours lookup | None | Query sanitized. OSM hours data often missing. |
| YouTube Music | `ytmusic_recent`, `ytmusic_search`, `ytmusic_library` | Recent history/catalog/library via `ytmusicapi` | None | Exposes personal listening history to the model; unofficial API can break. |

Internal bridge helpers not exposed as tools:

- `transcribe_voice` sends voice files to the configured Whisper endpoint.
- `record_share/current_location` stores user-shared location, reverse-geocodes
  through Nominatim, fetches current weather, and defers first mention to avoid
  creepy immediate callback.

## External MCP Servers

| Server | Command | Auth/config | Tools/capabilities | Gate/wrap behavior | Risks/gaps |
|---|---|---|---|---|---|
| `google_workspace` | `uvx --from google-workspace-mcp ...` | `GOOGLE_WORKSPACE_CLIENT_ID`, `GOOGLE_WORKSPACE_CLIENT_SECRET`, `GOOGLE_WORKSPACE_REFRESH_TOKEN` | Gmail, Calendar, Drive, Docs, Sheets, Slides | Read wildcard wrapped. Sends/replies/delete/create event/upload/delete folder defer. Bulk delete gatekeeper. | Main P1: wildcard lets unlisted upstream writes auto-run. Docs/Sheets/Slides writes are not explicitly gated. |
| `notion` | `npx -y @notionhq/notion-mcp-server` | `NOTION_TOKEN` | Search, data sources, pages, blocks, comments, users | Reads wrapped. Main page/block writes defer. | Token presence only; new upstream write tools can bypass explicit gates via wildcard. |
| `github` | `npx -y @modelcontextprotocol/server-github` | `GITHUB_PERSONAL_ACCESS_TOKEN` | Issues, PRs, repos, commits, releases, writes depending on PAT | Reads wrapped. Create issue/PR, merge, delete file/repo defer. | Prompt mentions comments/branches; not all possible state-changing tools are gated. |
| `playwright` | `npx -y @playwright/mcp@latest` | None | Browser automation for research fallback | Wrapped, no approval | Uses `@latest`; no dedicated live MCP smoke test. |
| `apple_events` | `npx -y mcp-server-apple-events` | macOS Automation permissions | Local Reminders/Calendar | Wrapped, intentionally ungated | Local writes can still be prompt-injection targets. Package unpinned. |
| `apple_shortcuts` | `npx -y mcp-server-apple-shortcuts` | macOS Automation permissions | User-authored Shortcuts | Wrapped, ungated | Shortcuts can be arbitrarily powerful depending on local shortcut definitions. Package unpinned. |
| `youtube_transcript` | `uvx --from git+...@v0.6.4 mcp-youtube-transcript` | None | YouTube transcript fetch | Wrapped, no approval | Pinned to tag, but only wiring tests. |
| `duckdb` | `uvx --from mcp-server-motherduck ... --db-path :memory:` | None | SQL analytics; attach SQLite read-only by convention | Wrapped, no approval; max rows 256, timeout 15s | Read-only relies on instructed `ATTACH ... READ_ONLY`; DuckDB memory DB itself is writable. |

## Google Workspace Detail

Known tool names in the specialist prompt:

- Calendar:
  - `calendar_get_events`
  - `calendar_get_event_details`
  - `create_calendar_event`
  - `delete_calendar_event`
- Gmail read:
  - `query_gmail_emails`
  - `gmail_get_message_details`
  - `gmail_get_attachment_content`
- Gmail write:
  - `create_gmail_draft`
  - `delete_gmail_draft`
  - `gmail_send_draft`
  - `gmail_send_email`
  - `gmail_reply_to_email`
  - `gmail_bulk_delete_messages`
- Drive:
  - `drive_search_files`
  - `drive_read_file_content`
  - `drive_upload_file`
  - `drive_create_folder`
  - `drive_delete_file`
  - `drive_delete_folder`
  - `drive_list_shared_drives`
- Docs:
  - `docs_create_document`
  - `docs_get_document_metadata`
  - `docs_get_content_as_markdown`
  - `docs_append_text`
  - `docs_prepend_text`
  - `docs_insert_text`
  - `docs_batch_update`
  - `docs_insert_image`
- Sheets:
  - `sheets_create_spreadsheet`
  - `sheets_read_range`
  - `sheets_write_range`
  - `sheets_append_rows`
  - `sheets_clear_range`
  - `sheets_add_sheet`
  - `sheets_delete_sheet`
- Slides:
  - `get_presentation`
  - `get_slides`
  - `create_presentation`
  - `create_slide`
  - `add_text_to_slide`
  - `add_formatted_text_to_slide`
  - `add_bulleted_list_to_slide`
  - `add_table_to_slide`
  - `add_slide_notes`
  - `duplicate_slide`
  - `delete_slide`
  - `create_presentation_from_markdown`

Explicitly gated today:

- `gmail_send_email`
- `gmail_reply_to_email`
- `gmail_bulk_delete_messages` via gatekeeper
- `delete_calendar_event`
- `create_calendar_event`
- `drive_delete_file`
- `drive_delete_folder`
- `drive_upload_file`

Important gap:

- Draft deletion/sending, Drive folder creation, Docs mutations, Sheets
  mutations, and Slides mutations appear to fall through the wildcard unless
  upstream uses names different from the prompt. This should be treated as a P1
  policy gap.

## Scheduled and Indirect Tool Flows

| Flow | Tool usage | Notes |
|---|---|---|
| Daily check-in | Uses internal control prompts to `drive_gmail` for Gmail buckets and calendar events | Fetch failures return empty shapes. Morning prompt asks whether to check emails/calendar. |
| Calendar heartbeat | Internal control prompt to `drive_gmail` calendar; wraps event title before composing visible proactive | Gated by config, cadence, and prior-notification signatures. |
| Reminder firing | Sends `reminder: <text>` directly through Telegram, then marks fired and schedules repeats | No LLM for fired reminder body. |
| GCal reminder sync | Internal control prompt to `drive_gmail` to call `create_calendar_event` | This uses gated Google create-event behavior through control path. |
| Apple reminder sync | Internal control prompt calls `mcp__apple_events__reminders_tasks` directly | Apple Events writes intentionally ungated. |
| Decision resolver | Weekly calibration check asks yes/no for overdue decisions | Resolution tool is direct and immutable. |
| External MCP server | Exposes read-only memory/wiki surfaces over Streamable HTTP | Uses bearer or OAuth, wraps and audits tool outputs. |

## Operator Scripts and Utilities

| Script/utility | Purpose | Inputs/config/env | Side effects | Notes |
|---|---|---|---|---|
| `scripts/backup.sh` | Daily SQLite backup | `data/hikari.db`, `sqlite3`, iCloud vault path | Writes dated DB backup, prunes after 14 | Uses SQLite `.backup` for WAL correctness. Exits nonzero if `sqlite3` missing despite "never raise" comment. |
| `scripts/install_backup.sh` | Install backup LaunchAgent | macOS `launchctl` | Writes `~/Library/LaunchAgents/com.hikari.backup.plist` | Repeatable; no confirmation before replacing plist. |
| `scripts/install_launchd.sh` | Install bot LaunchAgent | `uv`, macOS `launchctl` | Writes/starts `com.hikari.agent.plist` | Still relies on runtime `.env`/env setup; does not wire Keychain shim. |
| `scripts/setup_google_oauth.py` | Mint Google OAuth refresh token | `secrets/google_oauth_client.json`, localhost `:8910` | Prints client id/secret/refresh token | Broad scopes; secrets land in terminal output. |
| `scripts/migrate_secrets_to_keychain.py` | Move allowlisted `.env` secrets to macOS Keychain | `.env`, `security` CLI | Writes Keychain generic passwords | Has `--dry-run` and round-trip verification. |
| `scripts/migrate_from_current.py` | Old markdown bot data to SQLite | old bot user folder, optional `--fresh` | Writes core blocks, facts, episodes, tasks, thoughts | Mostly idempotent; `--fresh` is destructive and has no backup guard. |
| `scripts/ingest_to_memory.py` | Seed hardcoded memory dump | hardcoded constants, embeddings backend | Writes core blocks/facts/episodes/vectors | Can become stale; idempotent checks reduce duplicates. |
| `scripts/backfill_embeddings.py` | Embed facts/episodes missing vectors | live DB, embeddings config | Writes vector rows | No dry-run; may download/use model backend. |
| `scripts/reset_proactive_log.py` | Clear proactive cadence logs | none | Sets three runtime keys to `[]` | Useful for tests, easy to reset live cadence accidentally. |
| `scripts/upload_stickers.py` | Print sticker upload checklist | sticker manifest | No upload; prints operator steps | Safe, read-only. |
| `scripts/regen_mcp_json.py` | Generate `.mcp.json` from registry | `config/tools.yaml`, optional `--check` | Writes `.mcp.json` unless check mode | CI-friendly drift check. |
| `scripts/validate_tool_registry.py` | Validate registry consistency | registry, discovered handlers, `.mcp.json` | Exit 0/1 | Imports project modules during discovery. |
| `scripts/install_cloudflared.md` | Cloudflare Tunnel runbook | tunnel/domain/secrets/config | Manual ops | Drift: doc says bearer secret required, but code supports OAuth-only. |
| `scripts/install_lulu_rules.md` | LuLu egress allowlist runbook | LuLu app | System firewall rules | Broad Python/Google allowances need periodic review. |
| `scripts/setup_ytmusic.md` | YT Music browser auth setup | raw browser request headers | Writes cookie-like secret via documented command | Raw headers can leak into shell history. |

## External Remote MCP (`mcp_external`)

Exposes five read-only tools to Claude Desktop/iPhone over Streamable HTTP:

- `hikari_recall`
- `hikari_lexicon_top`
- `hikari_observations`
- `hikari_open_loops`
- `hikari_wiki_search`

Security posture:

- Server refuses to start unless `mcp_external.enabled` is true and at least
  one auth path is configured.
- Auth paths:
  - static bearer token via `HIKARI_MCP_SECRET`
  - OAuth 2.1 + PKCE + Dynamic Client Registration via owner passphrase
- OAuth uses:
  - signed `/authorize` state cookie
  - S256-only PKCE
  - opaque token storage in SQLite
  - refresh token rotation/family revocation
  - in-memory passphrase rate limiting
- Tool results are wrapped untrusted and audit logged.

Gap:

- Non-HTTP ASGI scopes pass through unauthenticated. This is acceptable for the
  current Streamable HTTP deployment but should be revisited if WebSockets or
  other transports are added.

## Test Coverage Observed

Strongest coverage:

- runtime allowlist regression
- removal of unscoped `Read`, `Glob`, `Grep`
- tool registry loading and generated `.mcp.json`
- defer and gatekeeper approval flows
- exact `CONFIRM-SEND`, implicit cancel, timeouts, restart recovery
- PostToolUse untrusted wrapping, including flat string outputs
- wiki and attachment path traversal
- Google/Notion/GitHub destructive gating
- OAuth and external MCP auth
- DuckDB and YouTube transcript wiring

Reported focused security suite result:

- `115 passed, 1 warning`

Weakest coverage:

- live upstream MCP tool drift
- broad external wildcard write surfaces
- live Playwright behavior
- Docs/Sheets/Slides/Drive/Gmail draft write policy
- prompt-vs-registry drift
- local ungated write/deletion tools
- `AUTH_PRECHECK=enforce` production behavior

## Findings

### P1 - Google Workspace wildcard can auto-allow unlisted writes

`mcp__google_workspace__*` is necessary for flexible upstream access, but today
it also means unlisted writes fall through as `gate: null`. The prompt lists
many Docs, Sheets, Slides, Drive, and Gmail write tools that are not explicitly
gated.

Recommendation:

- Add explicit registry entries for every known Google Workspace write tool.
- Default unknown Google Workspace tool names to deny or gate if they match
  write-like verbs: `create`, `update`, `delete`, `send`, `reply`, `upload`,
  `append`, `prepend`, `insert`, `write`, `clear`, `duplicate`, `batch_update`.
- Add tests that compare the specialist prompt's listed write tools against
  registry gates.

### P1 - External wildcard grants make upstream drift dangerous

Notion, GitHub, Playwright, Apple Events, Apple Shortcuts, YouTube transcript,
and DuckDB are wildcard allowlisted. This is ergonomic, but upstream package
updates can add tools without a registry review.

Recommendation:

- Add a registry concept of `operation: read | write | destructive | execute`.
- Require `gate` and `untrusted_output` classification for every known upstream
  write/execute tool.
- For wildcard catch-alls, add fail-closed heuristics for write-like names or
  require a documented rationale.

### P1 - Registry validation is too shallow for a safety boundary

Current validation catches file presence and `.mcp.json` drift, but not:

- invalid gate enum values
- duplicate ids
- invalid regex patterns
- subagent tool references with no registry coverage
- wildcard shadowing surprises
- external write tools that only match broad wildcard entries
- prompt claims that disagree with registry gates

Recommendation:

- Extend `scripts/validate_tool_registry.py` and `ToolRegistry.validate()` with
  these invariants.
- Make the validator a required ship gate.

### P1 - Subagent prompts drift from runtime policy

Examples:

- `drive_gmail.prompt.md` says calendar adds and Drive uploads auto-run, but
  `create_calendar_event` and `drive_upload_file` are gated.
- `github.description.md` mentions comments and branches, but only a subset of
  state-changing GitHub operations are explicitly gated.

Recommendation:

- Generate capability/gate summaries from the registry and inject them into
  subagent prompts.
- Add tests that fail when prompt claims disagree with registry policy.

### P2 - Utility auto-discovery bypasses explicit policy classification

Every utility tool exported in `ALL_TOOLS` is allowlisted automatically. That
makes adding tools pleasant, but a new local write/delete/network tool can be
exposed without explicit `gate`, `untrusted_output`, or scope rationale.

Recommendation:

- Keep auto-discovery for registration, but require each discovered utility
  tool to have an explicit policy entry or a documented local-read-only default.
- Fail validation for auto-discovered tools that look write-like and lack
  explicit metadata.

### P2 - Local ungated writes deserve a written policy

Ungated local writes include:

- `wiki_append`
- `note_create`
- `reminder_create`
- `receipt_add`
- `receipt_set_note`
- `receipt_delete`
- `link_save`
- `link_update`
- `link_delete`
- memory/core block writes

Some are intentionally low-friction. Some are permanent or context-shaping.

Recommendation:

- Define a local-write policy tier:
  - low-risk log/capture
  - reversible local write
  - irreversible delete
  - context/persona/memory mutation
  - external side effect
- Consider audit or soft-delete for `receipt_delete`, `link_delete`, and core
  memory mutations.

### P2 - Link shelf fetch can touch arbitrary network targets

`link_save` fetches arbitrary http(s) URLs with redirects and no private-IP or
localhost block.

Recommendation:

- Block loopback, private, link-local, multicast, and file-like redirects.
- Cap redirects and final content type.
- Add untrusted wrapping metadata for link shelf search/list/save outputs.

### P2 - Some manually wrapped tools are also PostToolUse-wrapped

`wiki_read` and `read_codex_report` manually call `wrap_untrusted` and are also
configured for PostToolUse wrapping. This is safe but noisy.

Recommendation:

- Pick one wrapping layer per tool. Prefer generic PostToolUse wrapping for
  consistency unless the tool result must include raw body in `data`.

### P2 - External packages should be pinned

Unpinned:

- `@playwright/mcp@latest`
- `mcp-server-apple-events`
- `mcp-server-apple-shortcuts`
- `@notionhq/notion-mcp-server`
- `@modelcontextprotocol/server-github`
- `google-workspace-mcp`
- `mcp-server-motherduck`

Partially pinned:

- `youtube_transcript` uses a git tag.

Recommendation:

- Pin package versions or lock through a controlled wrapper.
- Add a periodic "upstream tool diff" script that records tool names and flags
  additions/removals.

### P2 - Scope precheck is shadow by default

`AUTH_PRECHECK=shadow` logs scope deficits but does not block. That is fine for
rollout, not for a hard production access-control gate.

Recommendation:

- Set `AUTH_PRECHECK=enforce` in production once false positives are resolved.
- Fail closed for known destructive external tools when provider scope checks
  error.

### P3 - Documentation drift in ops runbooks

`scripts/install_cloudflared.md` says `HIKARI_MCP_SECRET` is required, but
`mcp_external.launch` supports OAuth-only mode with
`HIKARI_OAUTH_OWNER_PASSPHRASE`.

Recommendation:

- Update the runbook to say "set at least one auth path; set both for bearer
  and OAuth support."

## Recommended Next Pass

1. Add explicit Google Workspace write tool registry entries and gates.
2. Extend registry validation to enforce gate enums, regex validity, duplicate
   ids, subagent refs, and write-like wildcard policy.
3. Generate subagent capability/gating text from the registry to prevent prompt
   drift.
4. Add a script to enumerate live upstream MCP tool names and diff against a
   checked-in snapshot.
5. Pin external MCP package versions or add an upgrade review workflow.
6. Decide local write tiers and add audit/soft-delete for irreversible local
   deletes.
7. Harden `link_save` against SSRF/private-network fetches.
8. Turn on `AUTH_PRECHECK=enforce` after validating Google scope behavior.

## Verification Notes

- This report was built from static source review plus four parallel read-only
  explorer passes.
- One explorer ran the focused gating/security suite and reported
  `115 passed, 1 warning`.
- Parent-session registry verification passed with:
  `UV_CACHE_DIR=/private/tmp/uv-cache uv run python scripts/validate_tool_registry.py`
  -> `validate_tool_registry: clean.`
