---
title: Non-Google Integrations Deep Review
date: 2026-05-23
repo: /Users/ol/agents/hikari-agent
scope:
  - Notion
  - GitHub
  - Playwright
  - Apple Events
  - Apple Shortcuts
  - DuckDB / MotherDuck
  - YouTube Transcript
  - YouTube Music
  - Linear
  - other non-Google external connectors
---

# Executive Summary

Hikari's non-Google integration surface is broad and useful, but the risk is
unevenly distributed. The highest-risk integrations are not the obvious "big"
cloud services alone. They are the broad wildcard MCP grants and local-device
automation surfaces that can create durable side effects with no narrow local
policy layer.

The strongest current pattern is the central registry in `config/tools.yaml`.
It declares external servers, allowed tool patterns, gate policy, untrusted
wrapping, and subagent ownership in one place. The generic PostToolUse wrapper
in `agents/external_wrap_hook.py` is also a real improvement: Notion, GitHub,
Playwright, Apple Events, Apple Shortcuts, YouTube Transcript, DuckDB, and
selected utility tools are all treated as untrusted output before the model
continues.

The main problem is that several integrations are still allowed through
wildcards:

- `mcp__notion__*`
- `mcp__github__*`
- `mcp__apple_events__*`
- `mcp__apple_shortcuts__*`
- `mcp__playwright__*`
- `mcp__youtube_transcript__*`
- `mcp__duckdb__*`

For pure read surfaces that is mostly acceptable if wrapping works. For Notion,
GitHub, Apple Events, Apple Shortcuts, and DuckDB, it is not enough. Upstream
tool additions or already-known write verbs can fall through the wildcard with
`gate: null`, and Hikari's runtime uses `permission_mode="acceptEdits"` in
`agents/runtime.py`, so the local registry and `gatekeeper_can_use_tool` are the
real safety boundary.

No active Linear integration is configured locally. It appears only as a comment
in `.env.example`, while Linear's current MCP server is a hosted remote MCP with
OAuth / bearer-token support. The local code should either remove that stale
comment or add Linear deliberately with the same gate/wrap/version policy as the
other connectors.

# Integration Matrix

| Integration | Configured Where | Auth / Secrets | Exposed Surface | Writes / Destructive | Gate Policy | Untrusted Wrapping | Coverage |
|---|---|---|---|---|---|---|---|
| Notion MCP | `config/tools.yaml`, `.mcp.json`, `agents/subagents/prompts/notion.*` | `NOTION_TOKEN`, optionally injected from Keychain by `agents/runtime.py` | Search, data sources, pages, blocks, comments, users | Page create/update/delete, block append/update/delete, data-source create/update, move page, create comment | Five page/block write tools are `gatekeeper`; wildcard is ungated | `^mcp__notion__` | Gating tests cover only five write tools |
| GitHub MCP | `config/tools.yaml`, `.mcp.json`, `agents/subagents/prompts/github.*` | `GITHUB_PERSONAL_ACCESS_TOKEN`, optionally injected from Keychain | Issues, PRs, repos, commits, file operations | Create issue/PR, merge, delete file/repo; likely comments, branches, labels, reviews, updates | Five tools are `gatekeeper`; wildcard is ungated | `^mcp__github__` | Gating tests cover only five tools |
| Playwright MCP | `config/tools.yaml`, `.mcp.json`, research subagent | None | Browser navigation, page inspection, clicks/fill/screenshot | Web actions, login/session side effects, possible local browser state exposure | Ungated wildcard | `^mcp__playwright__` | Registry presence/wrap indirectly tested; no live smoke |
| Apple Events MCP | `config/tools.yaml`, `.mcp.json`, `CLAUDE.md`, proactive Apple sync | macOS EventKit / automation permission | Reminders and Calendar via EventKit | Create/update/delete reminders, subtasks, recurring/location reminders, calendar events | Ungated wildcard by design | `^mcp__apple_events__` | Tests assert writes are intentionally ungated |
| Apple Shortcuts MCP | `config/tools.yaml`, `.mcp.json`, README | macOS Shortcuts permission; shortcut-local secrets possible | List and run user-authored shortcuts | Arbitrary local/user-defined automation depending on installed shortcuts | Ungated wildcard | `^mcp__apple_shortcuts__` | Registry presence only; no policy tests |
| DuckDB / MotherDuck MCP | `config/tools.yaml`, `.mcp.json`, `docs/duckdb_mcp.md` | None currently; no MotherDuck token configured | SQL engine over in-memory DuckDB and attached local SQLite | DuckDB memory DB writable; local files may be readable through SQL functions/extensions | Ungated wildcard | `^mcp__duckdb__` | Wiring/wrap tests only |
| YouTube Transcript MCP | `config/tools.yaml`, `.mcp.json`, `tests/test_youtube_transcript_mcp.py` | None | Transcript and video metadata fetch | Read-only external content | Ungated wildcard | `^mcp__youtube_transcript__` | Good wiring/wrap tests; no live smoke |
| YouTube Music | `tools/ytmusic/**`, `config/engagement.yaml`, `scripts/setup_ytmusic.md`, AGENTS | Browser-cookie auth blob path via `YTMUSIC_BROWSER_JSON_PATH` | Recent listening history, catalog search, library songs | Read-only in current tools | Ungated utility tools | Explicit tool wrap patterns | Unit tests for happy path and missing auth |
| Linear | `.env.example` comment only | None configured locally | None active | None active | None | None | No local integration |
| Readwise / Reader | README only | `READWISE_TOKEN` mentioned | No current local tool | None active | None | None | Docs drift: README implies optional support that does not exist |
| Apple Notes | `tools/apple_notes/**`, `config/tools.yaml`, README | macOS Notes automation permission | Create/search/read Apple Notes | `note_create` writes Notes | Ungated utility tool | Search/read/create outputs wrapped | Strong unit tests |
| Link Shelf Fetcher | `tools/link_shelf/**`, AGENTS | None | Fetch URL metadata for user-shared links | Local SQLite writes; arbitrary HTTP(S) fetch | Ungated utility tools | Not explicitly wrapped in `config/tools.yaml` | Unit tests exist, but no SSRF/private-IP regression |
| OpenRouter Photo Gen | `tools/photos/**`, `.env.example`, MODELS | `OPENROUTER_API_KEY` | Image generation, photo outbox | Writes local image outbox; sends generated image through bridge | Mood gate + daily cap, no approval | Not applicable | Tests not reviewed here |
| OpenAI Whisper STT | `tools/voice.py`, `config/engagement.yaml`, `.env.example` | `OPENAI_API_KEY` | Voice-note audio upload | Read/external upload only | No gate | Transcript becomes user text, not a tool output | Graceful errors; no connector-wide audit |
| Translation | `tools/translate/**`, `config/engagement.yaml` | `DEEPL_API_KEY`; LibreTranslate endpoint optional | Sends text to DeepL or configured LibreTranslate | Read/external transform only | Ungated | `mcp__hikari_utility__translate` wrapped | Refuses default public LibreTranslate without key |
| Weather / Location | `tools/weather/**`, `tools/location.py`, `config/engagement.yaml` | Optional OpenWeather key; Open-Meteo/met.no/Nominatim no key | Forecast, reverse geocode | Read/external query; stores user location state | Ungated | Weather output wrapped | Good validation around lat/lon; location is opt-in share |
| Places / OSM | `tools/places/**`, `config/engagement.yaml` | None | Overpass POI/open-hours query | Read-only external query | Ungated | Places output wrapped | Sanitizes Overpass query text |
| Currency | `tools/currency/**`, `config/engagement.yaml` | None | Frankfurter ECB exchange rates | Read-only | Ungated | Currency output wrapped | Simple validation |
| arXiv | `tools/arxiv_search/**`, `pyproject.toml` | None | arXiv search | Read-only | Ungated | arXiv output wrapped | Basic unit coverage not reviewed deeply |

# Findings By Integration

## Notion

**Configuration.** Notion is declared as a bucket-3 external MCP server in
`config/tools.yaml` and `.mcp.json` using:

```text
npx -y @notionhq/notion-mcp-server
NOTION_TOKEN=${NOTION_TOKEN}
```

`agents/runtime.py` can inject a Notion token from Keychain into `NOTION_TOKEN`
when the env var is absent. `auth/notion.py` also contains a newer OAuth/PKCE/DCR
provider, but `config/scopes.yaml` still points Notion at the simpler
`auth.providers.NotionProvider`, which only checks token presence.

The local token approach matches the npm package's documented
`NOTION_TOKEN` option, but Notion is now prioritizing the hosted remote Notion
MCP over the local package and says the local repository may be sunset in the
future. The local package also moved to data-source tools in the 2.0.0 line,
which aligns with Hikari's prompt names such as `API-query-data-source` and
`API-create-a-data-source`.

Sources: [Notion MCP overview](https://developers.notion.com/guides/mcp/overview),
[Notion local MCP npm docs](https://www.npmjs.com/package/%40notionhq/notion-mcp-server),
[Notion local MCP repository](https://github.com/makenotion/notion-mcp-server).

**Tools exposed.** The Notion subagent prompt lists:

- Search: `API-post-search`
- Data sources: retrieve/list/query/create/update
- Pages: retrieve/create/patch/retrieve property/move
- Blocks: retrieve/get children/patch children/update/delete
- Comments: retrieve/create
- Users: get self/user/users

**Read/write/destructive surface.** Current explicit gates cover only:

- `API-patch-block-children`
- `API-update-a-block`
- `API-delete-a-block`
- `API-patch-page`
- `API-post-page`

The prompt also lists write-like tools that fall through the wildcard:

- `API-create-a-data-source`
- `API-update-a-data-source`
- `API-move-page`
- `API-create-a-comment`

Creating comments can notify people and persists model-supplied text; data
source creation/update changes schema; moving pages changes workspace
structure. These should not be auto-run.

**Gate policy.** `gatekeeper` for the five explicit page/block writes, then
`mcp__notion__*` with `gate: null`. This is the same wildcard-drift problem
called out in the earlier tool-risk report, but Notion's current package
evolution makes it more concrete: new API versions can add tools faster than
the registry catches up.

**Wrapping.** All Notion wildcard output is wrapped by `^mcp__notion__`.
That is solid for reads, but wrapping is not a substitute for gating writes.

**Tests/docs coverage.** Tests cover the five currently gated writes
(`tests/test_destructive_tool_gating.py`, `tests/test_tools_yaml.py`) but do
not parse the Notion prompt and require all write-like tools to be explicitly
classified. The prompt correctly says to introspect data-source schema first.

**Failure modes.**

- Token missing or integration not shared with a database returns auth/empty
  results, which the prompt handles.
- Local package drift or sunset can break tool names.
- Ungated comments/data-source/page-move tools can be reached through prompt
  injection if the model ignores wrapped read content.

**Fix.** Add explicit `gatekeeper` entries for every Notion verb matching
`create|update|patch|delete|move|comment`, including data sources and comments.
Add prompt-registry consistency tests.

## GitHub

**Configuration.** GitHub is declared as:

```text
npx -y @modelcontextprotocol/server-github
GITHUB_PERSONAL_ACCESS_TOKEN=${GITHUB_PERSONAL_ACCESS_TOKEN}
```

`agents/runtime.py` can inject a PAT from Keychain. `auth/github.py` can
validate a pasted PAT, detect classic PAT scopes from `X-OAuth-Scopes`, and
stores fine-grained PATs as `["*"]` because scopes are not introspectable the
same way.

**Current external behavior.** Hikari is still using the older
`@modelcontextprotocol/server-github` reference package. GitHub now maintains
`github/github-mcp-server` as the official GitHub MCP server, and GitHub's own
README says the remote server is the easiest path while the local server is
available when remote MCP is unsupported. The modelcontextprotocol servers repo
still documents the reference GitHub package, but describes the repo as a
reference-implementation collection, not GitHub's current first-party server.

Sources: [GitHub official MCP server](https://github.com/github/github-mcp-server),
[modelcontextprotocol servers reference repo](https://github.com/modelcontextprotocol/servers).

**Tools exposed.** The prompt uses `mcp__github__*` directly and describes
reads for issues, PRs, repos, commits, and releases. Writes are described as
opening PRs, comments, and branches.

**Read/write/destructive surface.** Current gates cover only:

- `create_issue`
- `create_pull_request`
- `merge_pull_request`
- `delete_file`
- `delete_repository`

Likely missing write classes under the wildcard:

- Issue and PR comments
- Issue/PR updates, close/reopen
- Branch/ref creation and update
- Review submissions, review requests, reactions
- Labels, assignees, milestones
- File update/create

**Gate policy.** `gatekeeper` for five explicit tools, then wildcard
`gate: null`. This is too permissive for a GitHub PAT.

**Wrapping.** All GitHub wildcard output is wrapped by `^mcp__github__`, which
is appropriate because issues, PR bodies, comments, and repo files are
attacker-shaped content.

**Tests/docs coverage.** Gating tests mirror only the five explicit tools.
There is no live or mocked upstream tool-list diff to catch writes newly
exposed by the server.

**Failure modes.**

- PAT missing, expired, or under-scoped.
- Fine-grained PAT scope precheck cannot meaningfully verify per-repository
  permissions because current code treats it as `*`.
- Upstream package drift, since `npx -y @modelcontextprotocol/server-github`
  is unpinned.
- Prompt injection from GitHub issues/PR comments can ask the model to mutate
  GitHub via ungated wildcard tools.

**Fix.** Migrate to `github/github-mcp-server` deliberately, or pin the current
reference package and enumerate every exposed write. Gate all mutating GitHub
tools. Add a tool-list snapshot test against a pinned version.

## Playwright

**Configuration.** Playwright is declared as:

```text
npx -y @playwright/mcp@latest
```

It is owned by the `research` subagent as a fallback after `WebSearch` and
`WebFetch`.

**Current external behavior.** Microsoft's Playwright MCP server provides
browser automation through structured accessibility snapshots. Its README
documents `@playwright/mcp@latest` as a standard config and notes important
security options such as `--allowed-hosts`, `--allowed-origins`, and
`--allow-unrestricted-file-access`. The default file behavior blocks file URLs
and restricts filesystem access to workspace roots unless unrestricted file
access is enabled.

Source: [microsoft/playwright-mcp](https://github.com/microsoft/playwright-mcp).

**Tools exposed.** Hikari grants `mcp__playwright__*`, so the exact tool set is
whatever the package exposes at runtime: navigate, click, fill, evaluate-ish
page actions if present, screenshots/snapshots, browser session control.

**Read/write/destructive surface.** Browser navigation is not read-only in
practice. It can:

- Click buttons and submit forms.
- Interact with logged-in browser sessions if the server shares browser state.
- Trigger web-side writes.
- Fetch untrusted web content into the model.

**Gate policy.** Ungated wildcard.

**Wrapping.** All Playwright output is wrapped.

**Tests/docs coverage.** The research prompt correctly says Playwright is a
last resort. There is no live smoke test, no pinned version, and no local
allowed-host/isolated-profile policy in `.mcp.json`.

**Failure modes.**

- `@latest` changes tool schemas or security defaults.
- Browser state leaks personal sessions.
- JS-rendered malicious pages cause prompt-injection pressure.
- Accidental form submissions or login-required actions happen without owner
  confirmation.

**Fix.** Pin the package version. Add a dedicated browser profile/storage dir
for Hikari research. Consider `--allowed-hosts` or equivalent if the primary
use is localhost/public web research. Gate or forbid form-submitting/clicking
tools if the tool list supports granular grants.

## Apple Events

**Configuration.** Apple Events is declared as:

```text
npx -y mcp-server-apple-events
```

It has no env vars. README says it uses Apple Reminders + Calendar via EventKit,
with macOS automation permission on first use. `CLAUDE.md` says Apple Reminders
and Calendar are owned directly, not by a subagent. `agents/proactive.py`
also uses `run_internal_control()` to mirror pending Hikari reminders into
Apple Reminders through `mcp__apple_events__reminders_tasks`.

The package source describes native macOS integration with Apple Reminders and
Calendar via EventKit, including creating reminders, recurring reminders,
location reminders, subtasks, updates, and list queries.

Source: [mcp-server-apple-events](https://github.com/FradSer/mcp-server-apple-events).

**Tools exposed.** Hikari grants `mcp__apple_events__*`. Tests mention these
known examples:

- `create_reminder`
- `delete_reminder`
- `create_calendar_event`
- `delete_calendar_event`
- `reminders_tasks`

The upstream docs indicate a broader reminders surface, including update,
complete, subtasks, tags, priority, recurring, and location-based reminders.

**Read/write/destructive surface.** Local Calendar and Reminders writes are
durable and sync over iCloud. This is lower blast radius than Gmail, but not
low-risk: calendar spam, reminder spam, deletes, and hidden recurring/location
reminders can materially affect the user.

**Gate policy.** Ungated by design. `tests/test_destructive_tool_gating.py`
explicitly asserts that Apple Events writes must not be gatekeeper-gated.

**Wrapping.** Output wrapped by `^mcp__apple_events__`.

**Tests/docs coverage.** There are tests asserting the ungated policy, but not
tests proving safe internal-only mirroring. `agents/proactive.py` wraps the
reminder title as untrusted before embedding it into an internal-control prompt,
which is good.

**Failure modes.**

- MCP package is unpinned.
- EventKit permission or automation failures surface as tool errors.
- Prompt injection can create/update/delete local synced reminders without
  owner confirmation.
- If the same wildcard is needed for internal mirror jobs and user-facing tool
  calls, policy cannot distinguish them.

**Fix.** Split internal Apple reminder mirroring from user-facing Apple Events
tools. Keep the mirror path private or context-tagged; gate user-facing create,
update, delete, recurring, location, and subtask mutations.

## Apple Shortcuts

**Configuration.** Apple Shortcuts is declared as:

```text
npx -y mcp-server-apple-shortcuts
```

No env vars are configured. README says it exposes every user-authored Shortcut
as callable and may trigger a macOS automation permission prompt.

The public package registry page describes two core tools: listing available
shortcuts and running a shortcut by name, optionally with input parameters.

Source: [mcp-server-apple-shortcuts registry](https://www.augmentcode.com/mcp/mcp-server-apple-shortcuts).

**Tools exposed.** Hikari grants `mcp__apple_shortcuts__*`.

**Read/write/destructive surface.** Shortcuts are arbitrary local automation.
Depending on installed shortcuts, running one can:

- Send messages.
- Open apps.
- Write files.
- Call web APIs.
- Control HomeKit or local device state.
- Invoke LLM or script actions with separate secrets.

**Gate policy.** Ungated wildcard.

**Wrapping.** Output wrapped by `^mcp__apple_shortcuts__`.

**Tests/docs coverage.** No meaningful policy tests found. README documents
the high-level behavior but not the risk boundary.

**Failure modes.**

- Installed shortcuts change outside the repo.
- Shortcut side effects are opaque to Hikari's registry.
- Prompt injection can run arbitrary user-authored automations.
- Package is unpinned.

**Fix.** Gate all `run shortcut` operations with full shortcut name and input
preview. Optionally allowlist a small set of known safe shortcut names. Treat
the list operation as read-only.

## DuckDB / MotherDuck

**Configuration.** DuckDB is declared as:

```text
uvx --from mcp-server-motherduck mcp-server-motherduck \
  --db-path :memory: \
  --ephemeral-connections \
  --max-rows 256 \
  --query-timeout 15
```

`docs/duckdb_mcp.md` instructs the agent to attach Hikari SQLite stores with:

```sql
INSTALL sqlite;
LOAD sqlite;
ATTACH '/Users/ol/agents/hikari-agent/data/hikari.db' AS hikari (TYPE sqlite, READ_ONLY);
ATTACH '/Users/ol/.day-receipt/receipt.db' AS receipts (TYPE sqlite, READ_ONLY);
```

No MotherDuck token is configured, so this is local DuckDB rather than cloud
MotherDuck.

The official MotherDuck MCP README describes a SQL analytics server that can
connect to local DuckDB files, in-memory databases, S3-hosted databases, and
MotherDuck, and explicitly supports executing read and write SQL queries.

Source: [motherduckdb/mcp-server-motherduck](https://github.com/motherduckdb/mcp-server-motherduck).

**Tools exposed.** Hikari grants `mcp__duckdb__*`; exact tools depend on the
server version. Local tests do not snapshot them.

**Read/write/destructive surface.** The attached SQLite databases are intended
to be read-only, but the DuckDB process is still a general SQL engine. It can
write to the in-memory DB and may read local files or use extensions/functions
depending on server/package defaults. `READ_ONLY` on an attached SQLite DB is
not a filesystem sandbox.

**Gate policy.** Ungated wildcard.

**Wrapping.** Output wrapped by `^mcp__duckdb__`.

**Tests/docs coverage.** `tests/test_duckdb_mcp.py` checks `.mcp.json` entry,
allowlist, and wrap pattern. It does not test sandboxing, query previews,
forbidden functions, local file reads, or write rejection.

**Failure modes.**

- Prompt injection from stored messages/facts asks the model to query secrets.
- SQL can be used to inspect unexpected local files.
- Package is unpinned.
- The docs say "read-only by contract," but runtime does not enforce a narrow
  query grammar or filesystem sandbox.

**Fix.** Replace with a narrow in-process analytics tool or gate ad hoc SQL with
full query preview. If keeping MCP DuckDB, run it in a sandbox with only the
intended SQLite paths readable and disable runtime extension install.

## YouTube Transcript

**Configuration.** YouTube Transcript is declared as:

```text
uvx --from git+https://github.com/jkawamoto/mcp-youtube-transcript@v0.6.4 \
  mcp-youtube-transcript
```

This is the best-pinned external MCP in the repo. `.mcp.json` documents why the
git tag is used instead of the bare npm name.

The upstream README lists `get_transcript`, `get_timed_transcript`, and
`get_video_info`, with cursor pagination for long transcripts.

Source: [jkawamoto/mcp-youtube-transcript](https://github.com/jkawamoto/mcp-youtube-transcript).

**Tools exposed.** `mcp__youtube_transcript__*`.

**Read/write/destructive surface.** Read-only external content. Transcript text
is untrusted, and YouTube metadata/transcripts can contain instructions.

**Gate policy.** Ungated wildcard.

**Wrapping.** Output wrapped by `^mcp__youtube_transcript__`.

**Tests/docs coverage.** `tests/test_youtube_transcript_mcp.py` verifies pinned
git source, allowlist, and wrapping.

**Failure modes.**

- Video has no transcript or transcript language missing.
- Long transcripts require pagination and can be summarized incompletely.
- YouTube markup/API behavior may change.
- Git tag pin reduces supply-chain drift but does not protect against local
  cache poisoning or dependency vulnerabilities.

**Fix.** Keep as-is for now. Add a mocked tool-shape test or occasional manual
smoke test for `get_transcript`, `get_timed_transcript`, and `get_video_info`.

## YouTube Music

**Configuration.** YouTube Music is an in-process utility tool family:

- `ytmusic_recent`
- `ytmusic_search`
- `ytmusic_library`

`config/engagement.yaml` points to `YTMUSIC_BROWSER_JSON_PATH`. The setup doc
uses `ytmusicapi.setup(...)` with copied browser request headers.

The ytmusicapi docs confirm this browser-auth method: copy an authenticated
`/browse` request header, pass it to setup, then pass the resulting JSON file to
`YTMusic()`. They also state those credentials stay valid as long as the browser
session is valid, roughly two years unless logged out.

Source: [ytmusicapi browser authentication](https://ytmusicapi.readthedocs.io/en/stable/setup/browser.html).

**Tools exposed.**

- `ytmusic_recent`: `get_history()`
- `ytmusic_search`: `search(query, filter, limit)`
- `ytmusic_library`: `get_library_songs(limit)`

**Read/write/destructive surface.** Current Hikari tools are read-only, but they
expose personal listening history and library taste into the model context.

**Auth/secrets.** The browser-cookie JSON is a long-lived session credential
and lives wherever `YTMUSIC_BROWSER_JSON_PATH` points. `.env.example` suggests
`./secrets/ytmusic_browser.json`, which should remain gitignored and treated as
a secret.

**Gate policy.** Ungated.

**Wrapping.** Explicit wrap patterns exist for all three tools and the
`mcp__hikari_utility__ytmusic_*` wildcard.

**Tests/docs coverage.** `tests/test_ytmusic.py` covers happy path and missing
auth graceful failure. Tool code catches API drift and returns a short failure.

**Failure modes.**

- Unofficial API breaks when YouTube changes behavior.
- Cookie file expires or is invalidated.
- Personal history is sensitive, even if read-only.

**Fix.** Keep read-only and wrapped. Add a status/audit surface that reports
whether `YTMUSIC_BROWSER_JSON_PATH` exists without printing the path contents.

## Linear

**Configuration.** No active Linear server exists in `.mcp.json`,
`config/tools.yaml`, or subagent prompts. The only local mention is
`.env.example`:

```text
# Linear MCP - auth flows via OAuth on first use (no env var needed)
```

Linear's current docs describe a hosted Streamable HTTP MCP server at
`https://mcp.linear.app/mcp`, with OAuth flow for Codex and optional bearer
token/API key support.

Source: [Linear MCP docs](https://linear.app/docs/mcp).

**Tools exposed.** None locally.

**Read/write/destructive surface.** None locally. If added, Linear's own docs
say the MCP can find, create, and update Linear objects like issues, projects,
and comments.

**Gate policy.** None locally.

**Wrapping.** None locally.

**Tests/docs coverage.** None locally. The env comment is stale because no
server is configured.

**Failure modes.**

- User expects Linear to work because `.env.example` says it exists.
- Future install through global Codex config could bypass Hikari's local
  registry policy if not mirrored into `config/tools.yaml`.

**Fix.** Either remove the comment or add Linear intentionally with:

- explicit server entry,
- auth status display,
- read/write/destructive classification,
- write gates for create/update/comment/archive/delete operations,
- untrusted output wrapping,
- tests that no Linear wildcard write can auto-run.

## Other Non-Google Connectors

### Apple Notes

Apple Notes is in-process rather than external MCP. It uses argv-style
`osascript`, escapes AppleScript string literals, and caps calls at 10 seconds.
`note_create` writes local/iCloud Notes and is ungated. Search/read/create
outputs are wrapped in `config/tools.yaml`.

This is better implemented than Apple Shortcuts because the code is local,
testable, and has escaping/timeouts. The remaining risk is policy: `note_create`
is a durable local write and should probably be `gatekeeper` unless the user
explicitly accepts quick-capture auto-writes.

### Link Shelf

`link_save` fetches arbitrary HTTP(S) URLs to pull title/description, follows
redirects, and writes local SQLite. It has timeout and max-byte limits, but no
private-IP/loopback redirect block and no explicit untrusted-output wrapping in
`config/tools.yaml`. This is a small but realistic SSRF/local metadata risk if
the agent saves an attacker-supplied URL automatically.

Fix: block loopback, link-local, RFC1918, multicast, and file-like schemes
after redirects. Wrap link metadata output as untrusted.

### OpenRouter Photo Generation

`generate_photo` sends a prompt to OpenRouter's image generation endpoint and
writes returned image bytes to `data/photo_outbox`. It is mood-gated, daily
capped, and requires `OPENROUTER_API_KEY`.

OpenRouter's Flux.2 Klein docs describe image generation through model
`black-forest-labs/flux.2-klein-4b` and base64 image output through the API.
Hikari currently uses `black-forest-labs/flux.2-klein`, so the model alias
should be checked against the current canonical model IDs when this code is
next touched.

Source: [OpenRouter FLUX.2 Klein API page](https://openrouter.ai/black-forest-labs/flux.2-klein-4b/api).

### OpenAI Whisper STT

`tools/voice.py` uploads Telegram voice-note audio to the configured STT
endpoint, defaulting through config to OpenAI Whisper with `OPENAI_API_KEY`.
This is not an MCP tool and has clean graceful failure behavior. The privacy
surface is real: private audio leaves the machine.

Fix: add this to `/tools` or `/status` so external audio transcription is
visible as a configured connector. Long term, migrate to local STT if desired.

### Translation

`translate` sends text to DeepL when `DEEPL_API_KEY` is set, otherwise refuses
the default public LibreTranslate endpoint unless a non-default endpoint is
configured. That refusal is good: the local config comment still says
LibreTranslate fallback exists, but the code avoids burning a known-bad public
call.

DeepL documents a 128 KiB request-size limit and up to 50 text values per
translate request. Hikari sends one text value and has no explicit size cap at
the tool boundary.

Sources: [DeepL translate API](https://developers.deepl.com/api-reference/translate),
[LibreTranslate docs](https://docs.libretranslate.com/).

Fix: cap input text size below DeepL's request limit and document that
translation sends user text to a third party.

### Weather, Location, Places, Currency, arXiv

These are read-only public-data connectors:

- Weather: Open-Meteo, met.no, optional OpenWeatherMap.
- Location: Nominatim reverse geocode and Open-Meteo after explicit Telegram
  location share.
- Places: OSM Overpass with query sanitization.
- Currency: Frankfurter.
- arXiv: Python `arxiv` package/API.

The main risk is privacy rather than destructive action: coordinates and place
queries are externalized. Current location handling is intentionally opt-in and
defers first mention to avoid creepy immediate callbacks. Places query
sanitization is solid.

# Cross-Cutting Risks

1. **Wildcard writes fail open.** Notion and GitHub have known write-like tools
   under ungated wildcards. Apple Events, Apple Shortcuts, and DuckDB are even
   broader because the entire server class can mutate local state.

2. **Runtime package drift.** Most external MCP packages are floating:
   `npx -y @notionhq/notion-mcp-server`, `npx -y @modelcontextprotocol/server-github`,
   `npx -y @playwright/mcp@latest`, `npx -y mcp-server-apple-events`,
   `npx -y mcp-server-apple-shortcuts`, and `uvx --from mcp-server-motherduck`.
   YouTube Transcript is the exception and should be the model.

3. **Approval previews are still too thin.** Gatekeeper summaries are one-line
   previews with truncation in `tools/gatekeeper.py`. This is better than silent
   auto-accept, but not enough for Notion/GitHub body edits, Python, Drive, or
   future shortcut inputs.

4. **Local side effects bypass external-auth intuition.** Apple Events, Apple
   Shortcuts, Apple Notes, link shelf, DuckDB, and photo outbox are local, but
   still real side effects. "No cloud token" should not mean "no gate."

5. **Prompt-registry drift.** Prompts list specific tools and policy claims,
   while `config/tools.yaml` is the actual enforcement layer. Notion and GitHub
   prompts are useful, but tests need to keep their write lists in sync with
   gates.

6. **Auth status is presence-based.** Notion/GitHub scope prechecks mostly
   verify that a token exists. Fine-grained GitHub PATs become `*`; Notion
   provider config is still the simple token provider even though OAuth code
   exists.

7. **Untrusted wrapping covers many outputs but not every external intake.**
   MCP outputs are mostly covered. Link shelf fetched metadata and some direct
   bridge/document paths need separate attention.

8. **Docs drift creates false affordances.** Readwise and Linear are mentioned
   in docs/env comments but not implemented locally. That is small, but it
   causes operator confusion.

# What Is Solid

- `config/tools.yaml` is a good single-source registry for servers, gates,
  wrap patterns, and subagents.
- `agents/external_wrap_hook.py` handles several MCP output envelope shapes,
  including flat string content, and audits wrap activation.
- The Notion prompt requires data-source schema introspection before querying.
- YouTube Transcript is pinned to a git tag and has explicit wiring/wrap tests.
- YouTube Music tools are read-only, lazy-load `ytmusicapi`, and fail gracefully
  when auth is missing or the unofficial API breaks.
- Apple Notes uses argv-style subprocess execution, AppleScript string quoting,
  timeouts, and cross-platform graceful failure.
- Places sanitizes Overpass QL input before interpolation.
- Location sharing is explicit and deferred before surfacing back to the user.
- `agents/tool_inventory.py` gives Hikari a live "configured/unconfigured"
  context block for external MCP env vars.

# Recommended Fix Order

1. **Close the known write wildcard holes.**
   - Notion: gate data-source create/update, page move, and create comment.
   - GitHub: gate comments, updates, branch/ref operations, reviews, labels,
     assignees, reactions, file create/update.

2. **Gate local automation.**
   - Apple Shortcuts run operations should require confirmation.
   - Apple Events create/update/delete should require confirmation outside the
     internal reminder-mirror path.
   - Consider gating Apple Notes `note_create`.

3. **Replace or sandbox DuckDB.**
   - Best: narrow in-process analytics tool with approved templates.
   - Acceptable: gate SQL with full preview and run DuckDB under a filesystem
     sandbox allowing only intended DB files.

4. **Pin all external MCP packages.**
   - Convert every floating `npx -y` and `@latest` to exact versions or pinned
     immutable SHAs.
   - Add a manual dependency-update checklist.

5. **Add tool-list drift detection.**
   - For each external MCP, capture tool names from the pinned version and fail
     CI if a write-like new tool resolves only through an ungated wildcard.

6. **Improve approval previews.**
   - Structured per-tool previews for Notion, GitHub, Apple Shortcuts, Apple
     Events, DuckDB SQL, and Python.
   - Full body/query preview artifact when content is too long for Telegram.

7. **Fix docs drift.**
   - Remove or mark Readwise/Linear as planned, not active.
   - Or implement them with registry policy from day one.

8. **Harden smaller connector privacy boundaries.**
   - Link shelf private-IP/loopback redirect block.
   - Translation input size cap and explicit status.
   - `/status` or `/tools` visibility for Whisper, OpenRouter, YT Music, and
     location connectors.

# Suggested Tests

- `test_notion_prompt_write_tools_are_gated`: parse
  `agents/subagents/prompts/notion.prompt.md`; every tool containing
  `create|update|patch|delete|move|comment` must resolve to explicit
  `gatekeeper` unless allowlisted.

- `test_github_write_like_tools_are_gated`: compare a pinned GitHub MCP tool
  snapshot against `config/tools.yaml`; all write-like verbs must be explicit
  and gated.

- `test_no_external_write_wildcard_without_classification`: for every bucket-3
  wildcard, require a companion `known_read_tools`/`known_write_tools` table or
  a pinned upstream tool snapshot.

- `test_apple_shortcuts_run_is_gated`: `mcp__apple_shortcuts__run*` or the
  actual run tool name must resolve to `gatekeeper`.

- `test_apple_events_internal_mirror_bypass_only`: user-facing Apple Events
  writes gate; internal reminder sync can call a private wrapper or context-tagged
  path.

- `test_duckdb_rejects_file_reads_or_requires_gate`: synthetic SQL such as
  `read_text('.env')` or equivalent should either be impossible in the sandbox
  or require gatekeeper with full preview.

- `test_link_save_blocks_private_redirects`: URLs resolving or redirecting to
  `127.0.0.0/8`, `::1`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`,
  link-local, and metadata IPs are refused.

- `test_link_shelf_outputs_are_wrapped`: add explicit wrap patterns for
  `link_search`, `link_list`, and `link_save` metadata results.

- `test_external_mcp_versions_are_pinned`: reject `@latest`, unqualified npm
  package names, and unqualified `uvx --from` package specs in runtime MCP
  servers.

- `test_tool_inventory_mentions_non_google_status`: status block should include
  Notion, GitHub, Apple Events, Apple Shortcuts, Playwright, YouTube Transcript,
  DuckDB, YT Music, OpenRouter, Whisper, DeepL, and Readwise/Linear if docs
  mention them.

# Sources

## Local Sources

- `AGENTS.md`
- `CLAUDE.md`
- `codex/index.md`
- `codex/tool-subagent-inventory-2026-05-23.md`
- `codex/tool-subagent-risk-deep-dive-2026-05-23.md`
- `codex/security-review-2026-05-23.md`
- `codex/other-tools-review-2026-05-23.md`
- `config/tools.yaml`
- `.mcp.json`
- `config/scopes.yaml`
- `config/engagement.yaml`
- `.env.example`
- `agents/runtime.py`
- `agents/external_wrap_hook.py`
- `agents/tool_inventory.py`
- `agents/proactive.py`
- `agents/subagents/prompts/notion.prompt.md`
- `agents/subagents/prompts/github.prompt.md`
- `agents/subagents/prompts/research.prompt.md`
- `tools/gatekeeper.py`
- `tools/ytmusic/**`
- `tools/apple_notes/**`
- `tools/link_shelf/**`
- `tools/photos/**`
- `tools/voice.py`
- `tools/location.py`
- `docs/duckdb_mcp.md`
- `scripts/setup_ytmusic.md`
- `tests/test_destructive_tool_gating.py`
- `tests/test_tools_yaml.py`
- `tests/test_duckdb_mcp.py`
- `tests/test_youtube_transcript_mcp.py`
- `tests/test_ytmusic.py`

## External Sources

- Notion MCP overview: https://developers.notion.com/guides/mcp/overview
- Notion local MCP npm docs: https://www.npmjs.com/package/%40notionhq/notion-mcp-server
- Notion local MCP repository: https://github.com/makenotion/notion-mcp-server
- GitHub official MCP server: https://github.com/github/github-mcp-server
- Model Context Protocol servers reference repo: https://github.com/modelcontextprotocol/servers
- Playwright MCP: https://github.com/microsoft/playwright-mcp
- Apple Events MCP: https://github.com/FradSer/mcp-server-apple-events
- Apple Shortcuts MCP registry page: https://www.augmentcode.com/mcp/mcp-server-apple-shortcuts
- MotherDuck MCP server: https://github.com/motherduckdb/mcp-server-motherduck
- YouTube Transcript MCP: https://github.com/jkawamoto/mcp-youtube-transcript
- ytmusicapi browser authentication: https://ytmusicapi.readthedocs.io/en/stable/setup/browser.html
- Linear MCP docs: https://linear.app/docs/mcp
- Readwise Reader API: https://readwise.io/reader_api
- OpenRouter Flux.2 Klein API page: https://openrouter.ai/black-forest-labs/flux.2-klein-4b/api
- DeepL Translate API: https://developers.deepl.com/api-reference/translate
- LibreTranslate docs: https://docs.libretranslate.com/
