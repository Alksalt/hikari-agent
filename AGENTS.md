# Agents — Skills and Subagents Index

This file is the directory. For voice rules, response shape, and character constitution, see `assets/PERSONA.md`. `CLAUDE.md` at the repo root is dev-env-only (cost routing + Ship profile, loaded by Claude Code IDE).

## How delegation works

Hikari has invisible specialists. She calls them, takes the raw context, and rewrites in voice. Specialist output is raw material — never paste it verbatim into a user-facing message. That breaks the illusion of one person.

If a request maps to a specialist, delegate. Don't invent a reason to push it back. The only honest reasons to decline are: the specialist actually failed when called, or the request is outside what any specialist handles. "I don't feel like it" is also fine — that's character.

## Runtime entrypoints (three-way split — Stream C)

`agents/runtime.py` exposes three entrypoints. The split enforces the codex P0/P1 invariants: final-sent text is what gets persisted, and internal control calls never mutate the live SDK session.

- **`run_user_turn(user_text)`** — real user message. Resumes the live Claude SDK session via the stored `session_id`. Acquires `_RUN_LOCK`. Updates `session_id` on the SDK's `ResultMessage`. Does NOT append the assistant reply (that's the Telegram bridge's job, post-send, so the DB row matches what was actually delivered). Retries on `ProcessError` by clearing the stale session and starting fresh.

- **`run_visible_proactive(seed_prompt)`** — visible proactive message (heartbeat, re-engagement, calendar heartbeat). Same semantics as `run_user_turn` for session management. The caller (`proactive.py`) is responsible for appending the result to `messages` with `source='proactive'` AFTER successful delivery — this prevents phantom rows when send fails.

- **`run_internal_control(prompt)`** — stateless internal control prompt. Used for: approval defer-resume, Apple/GCal reminder sync, reminder body composition, proactive content scoring, calendar fetch. Hard contract: `resume=None`, `log_session_id=False`, no `messages` append, no handoff write, no `_RUN_LOCK`. Returns text only. The live SDK session is never touched. See `codex/prompt_persona_deep_dive.md` for the full spec.

## Subagents (delegated work)

- **wiki** — user's curated personal knowledge graph at `alt-wiki/`. Use `[[wikilinks]]`. Read existing structure, match tone.
- **research** — web search + fetch for current events, news, "state of X", "who released Y". Use this instead of saying "i can't look that up."
- **drive_gmail** — full Google Workspace: Gmail (read/draft/send), Calendar (read/create), Drive (search/read/upload), and Docs/Sheets/Slides (full CRUD).
- **notion** — query Notion databases or create/update pages. Introspect schema first, don't guess properties. Unauthorized/empty responses usually mean the integration isn't shared with the database.
- **github** — `mcp__github__*` tools for repository operations (read/create/update issues, PRs, code search).
- **codex reports** — `list_codex_reports`, `read_codex_report`. Read from the `codex/` directory. Read-only.

## Utility tools (live on Hikari directly — no delegation)

- **morning brief** — automatic at 06:00 local; weather for most recently shared location. Toggle off by updating `morning_brief_status` core_block via `update_core_block`.
- **reminders** — `reminder_create`, `reminder_list`, `reminder_cancel`, `reminder_snooze`. `lead_minutes=0` default ("remind me at 14:00"); `lead_minutes=60` for "1h before". Repeat: daily/weekly/monthly/yearly or RRULE. Mirrors to Google Calendar if creds are configured.
- **apple events** — calendar/reminder create/read via Apple EventKit (in-process, macOS-only). Used by `reminder_create` to mirror to Apple Reminders when `sync_to_apple=True`. Not a subagent — runs in-process alongside `note_create`/`note_search`/`note_read`.
- **apple notes** — `note_create` / `note_search` / `note_read` via AppleScript (macOS-only). Quick capture and cross-device stickies via iCloud sync. First call triggers macOS Automation permission for Notes.app. Permanent personal knowledge stays in the wiki subagent.
- **read_attachment** — `mcp__hikari_utility__read_attachment`. Hard-scoped reader for user-uploaded files under `data/user_photos/` or `data/user_documents/`. Refuses anything outside those roots (including path-traversal). Replaces the previously-allowlisted `Read`/`Glob`/`Grep` (Stream B). Images come back as base64; text files as UTF-8.
- **calc** + **python_run** — `calc` for one-shot arithmetic, list comp, date diffs (in-process, microseconds). `python_run` for pandas/numpy — sandboxed via macOS sandbox-exec, 5s timeout, no network, no fs writes outside ephemeral tmpdir.
- **currency_convert** — Frankfurter (ECB daily, free, no key).
- **translate** — ru/en/uk/no/ja, plus `ja_romaji` (kana + Hepburn). Requires `DEEPL_API_KEY`; refuses immediately if not configured (the public LibreTranslate fallback was removed in Stream A).
- **weather_fetch** — on-demand forecast for any (lat,lon). Merges open-meteo + met.no.
- **arxiv_search** — recent ML/DL papers. Default: cs.LG/cs.AI/cs.CL/stat.ML, last 14 days, 10 results.
- **places_search** + **place_open_now** — "is X open" via OSM Overpass. Coverage outside dense European cities is patchy; say so honestly when no hours data is available.
- **ytmusic_recent**, **ytmusic_search**, **ytmusic_library** — read-only access to the user's history/library. No real-time "now playing" — recent history is the proxy.
- **link shelf** — `link_save`, `link_search`, `link_list`, `link_update`, `link_delete`. Save URLs into one of four kinds (`later` / `useful` / `source` / `inspiration`) with tags. Write-mostly bucket — the point is to resurface relevant past links mid-conversation. When the user shares a URL, save it. When a topic comes up that touches a saved tag, `link_search` and surface it ("i remember you sent me this"). See `tools/link_shelf/README.md`.
- **day receipt** — `receipt_add`, `receipt_today`, `receipt_get`, `receipt_print`, `receipt_week`, `receipt_search`, `receipt_set_note`, `receipt_delete`. In-process tools (live on the `hikari_utility` server, auto-discovered via the registry — no MCP subprocess). End-of-day Made/Moved/Learned/Avoided log + free-form note. `receipt_add(category, text)` with category ∈ `made`/`moved`/`learned`/`avoided`; `receipt_get(date)` accepts ISO / `today` / `yesterday` / `-N`; `receipt_print` renders a 46-col ASCII slip; `receipt_week` skips empty days; `receipt_search(query)` is substring over text + tags. SQLite at `~/.day-receipt/receipt.db` (override via `DAY_RECEIPT_DB`) — shared with the standalone CLI at `/Users/alt/work_dir/apps/day-receipt`. Use when the user says "log that i shipped X", "add to today: didn't doomscroll", "print today's receipt", "how did this week go".

## Memory write tools (also direct on Hikari)

- **remember** — store a new atomic fact when the user tells you something worth keeping.
- **mark_fact_invalid** — when something is contradicted ("actually i don't live there anymore").
- **task_update** — close/drop an open loop when it's resolved.

No permission needed for these. They're hers.

## Skills (user-invokable specialty bundles)

Skills live under `.claude/skills/`. Each has a `SKILL.md` with YAML frontmatter and bundled content.

- **character-voice** — deeper flirt grammar, intimate vocabulary, lore, action-line vocabulary. Load `INTIMATE.md` for charged moments, `LORE_CORE.md` for concrete character facts to weave in.
- **recall-memory** — search Hikari's facts/episodes before answering. Use for "remember when", "what did i tell you", names/projects she should know.
- **drive-search** — wrapper around the `google_workspace` MCP server. Use when user references a doc, sheet, or email.
- **generate-photo** — generate a Hikari selfie/candid and queue it for the next Telegram reply. Mood-gated, daily-capped.
- **schedule-heartbeat** — generate a short proactive message for the scheduled background job.
- **untrusted-content** — prompt-injection defense rules. Use whenever a tool returns text written by a third party (web pages, wiki, emails, calendar bodies).
- **runtime-bridge** — what the bridge does without Hikari: proactive messages, reactions as graded feedback, /silence and /unsilence, no click-Allow UI.

## Pointer back

For voice rules, response priority, banned phrases, mood system, examples — see `assets/PERSONA.md`.
