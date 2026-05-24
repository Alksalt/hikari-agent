# Second-Pass Review: Google Workspace / Tool Surface

Date: 2026-05-24
Workspace: `/Users/ol/agents/hikari-agent`

## 1. Current-state summary

The current Google Workspace surface is materially more registry-driven and fail-closed than the older notes imply. `google_workspace` is pinned to `google-workspace-mcp==2.0.1` in `config/tools.yaml:75` and the generated `.mcp.json:45` matches it. Explicit Google read tools are listed with scopes and `untrusted_output: true` in `config/tools.yaml:966`, while known Google write/destructive tools are explicit `gatekeeper` entries. Unknown future `mcp__google_workspace__*` tools now resolve through a wildcard with `access_mode: write` in `config/tools.yaml:1128`, and `tools/gatekeeper_can_use_tool.py:168` denies wildcard write/destructive calls unless an explicit gated entry is added.

Auth precheck is enabled by config (`config/engagement.yaml:871`) and hook resolution honors `AUTH_PRECHECK_OVERRIDE` > `AUTH_PRECHECK` > config at `agents/hooks.py:676`. Google granted scopes are probed via OAuth token refresh plus `tokeninfo` in `auth/google.py:137`, and scope supersets for Gmail, Calendar, and Drive are modeled in `auth/scope_match.py:14`.

The daily calendar fetch path is now a typed direct adapter: `agents/daily_checkin.py:358` calls `tools/calendar/get_events.py:103`, which calls the Google Workspace MCP manager directly and parses structured events. The daily email check-in path remains prompt/subagent-mediated through `run_internal_control` and strict YAML parsing at `agents/daily_checkin.py:291`.

Verification run:

- `uv run python -m pytest tests/test_google_workspace_send_policy.py tests/test_tool_policy_access_mode.py tests/test_auth_scopes.py tests/test_scope_match.py tests/test_typed_calendar_adapter.py tests/test_daily_checkin_fetch.py tests/test_mcp_pinning.py tests/test_tools_yaml.py tests/test_subagent_prompt_policy_drift.py tests/test_approval_preview_truthful.py tests/test_google_health.py tests/test_reminders_scheduler.py tests/test_post_filter_fabrication.py`
- Result: 170 passed, 1 warning.
- `uv run python scripts/validate_tool_registry.py`
- Result: `validate_tool_registry: clean.`
- `uv run python scripts/regen_mcp_json.py --check`
- Result: `.mcp.json is up to date.`

## 2. Findings, ordered P0/P1/P2/P3

### P0

No P0 findings in this pass.

### P1

#### P1-1. Gatekeeper approval previews for Google writes omit consent-critical payload fields

`tools/gatekeeper_can_use_tool.py:22` defines critical fields including `to`, `cc`, `bcc`, `body`, `html_body`, `content`, `text`, `subject`, `values`, and `range`, and the fallback renderer is explicitly designed to show critical fields in full. However, `_summarize()` gives per-tool summarizers precedence at `tools/gatekeeper_can_use_tool.py:116`, and the Google-specific summaries in `tools/gatekeeper.py` are much thinner:

- `tools/gatekeeper.py:287` shows Gmail send `to` and truncated subject only; it does not show `body`, `html_body`, `cc`, `bcc`, or attachments.
- `tools/gatekeeper.py:292` shows only the message id and the first 80 chars of reply body.
- `tools/gatekeeper.py:306` shows calendar event title and start only; it omits end time, description, attendees, conference/link fields, and location.
- `tools/gatekeeper.py:315` shows only a Drive upload name; it omits local path/source, destination folder, MIME/content details, and any overwrite-like arguments the upstream tool may expose.

Impact: the owner can type `CONFIRM-SEND` without seeing the actual outbound email/reply body or full event/upload payload. That weakens the safety property of the gatekeeper path specifically for high-impact Google Workspace actions.

Suggested fix: either remove the Google-specific short summaries so the critical-field fallback renders the payload, or rewrite them to include all consent-critical fields with the same overflow/refusal behavior as the fallback. Add tests that assert Gmail send/reply/draft, calendar create, Drive upload, Docs/Sheets/Slides writes expose critical fields or refuse overly large payloads.

### P2

#### P2-1. Calendar fabrication backstop does not recognize the current typed calendar adapter

The current allowed typed adapter is `mcp__hikari_utility__calendar_get_events` in `config/tools.yaml:567`, and the tool implementation is `tools/calendar/get_events.py:129`. But the outgoing fabrication guard only counts names beginning with `mcp__google_workspace__calendar_` as legitimate calendar fetches (`agents/post_filter.py:125`, checked at `agents/post_filter.py:188`).

Impact: if a normal chat turn uses the typed adapter and then says something like "you have 2 meetings today", the post-filter can replace the true answer with `give me a sec — let me actually check.` because it does not recognize the actual tool that checked. The daily proactive path is mostly unaffected because `filter_outgoing` is not applied there, but live chat calendar checks can false-positive.

Suggested fix: include `mcp__hikari_utility__calendar_get_events` in the calendar fetch allowlist and add a test parallel to `tests/test_post_filter_fabrication.py:130` for the typed adapter.

#### P2-2. Google reminder sync job is decided before the fresh Google credential probe runs

At startup, the bridge builds and starts the scheduler at `agents/telegram_bridge.py:2374`. Only after that does it run `probe_google_token()` and write `runtime_state.calendar_heartbeat_healthy` at `agents/telegram_bridge.py:2393`. The scheduler decides whether to add `reminders_gcal_sync` during construction (`agents/scheduler.py:62`) by calling `_calendar_creds_healthy()`, which trusts an existing runtime-state value if present (`agents/scheduler.py:398`).

Impact: a stale `0:invalid_grant` row can cause the Google Calendar reminder sync job to be skipped for the entire process even after the same startup probe later proves credentials healthy. Conversely, a stale `1` can add the job before the fresh probe marks credentials unhealthy. Local reminders still fire, and pending Google mirrors accumulate, so this is reliability rather than direct data loss.

Suggested fix: run the Google probe before `build_scheduler(send_text)`, or have the scheduler job check credential health at execution time and no-op with a clear log. Add startup-order tests for stale unhealthy -> healthy and stale healthy -> unhealthy.

#### P2-3. `drive_gmail` subagent prompts contradict the registry’s current gatekeeper policy

The subagent prompt says "Drafts, calendar adds, and Drive uploads auto-run" at `agents/subagents/prompts/drive_gmail.prompt.md:1`, and the description repeats it at `agents/subagents/prompts/drive_gmail.description.md:1`. Current registry state says otherwise: `create_calendar_event` is gatekeeper-gated at `config/tools.yaml:647`, `drive_upload_file` at `config/tools.yaml:669`, and Gmail draft create/delete/send at `config/tools.yaml:680`.

The same prompt also says the names are exports of `google-workspace-mcp 1.27+` at `agents/subagents/prompts/drive_gmail.prompt.md:3`, while the current configured package is `google-workspace-mcp==2.0.1` (`config/tools.yaml:75`).

Impact: the runtime gate still catches the calls, but the specialist can give the lead/operator the wrong expectation about whether confirmation will happen. It also leaves drift tests too narrow: `tests/test_subagent_prompt_policy_drift.py:37` only tracks five bare write names and does not compare prompt claims to all Google `gatekeeper` entries.

Suggested fix: update the prompt/description from the registry or generate this policy block. Expand the drift test so every Google Workspace `gatekeeper` entry is either listed as gated or the prompt avoids tool-specific gate claims entirely.

### P3

#### P3-1. Cockpit reports the wrong default auth precheck mode

`agents/cockpit.py:78` reads `AUTH_PRECHECK`, then `AUTH_PRECHECK_OVERRIDE`, then defaults to `shadow`. The actual hook priority is `AUTH_PRECHECK_OVERRIDE` > `AUTH_PRECHECK` > `config auth.precheck` > `shadow` (`agents/hooks.py:676`), and config currently sets `auth.precheck: enforce` (`config/engagement.yaml:871`).

Impact: `/settings get AUTH_PRECHECK` can report `shadow` on a process where scope precheck is actually enforcing. This is an operator observability issue, not a tool-call bypass.

Suggested fix: mirror the hook priority in `_read_auth_precheck()` and add a test with no env vars and config `auth.precheck: enforce`.

## 3. Previously reported issues that now look closed

- External Google Workspace MCP pinning is closed in current state: `config/tools.yaml:75` and `.mcp.json:45` both pin `google-workspace-mcp==2.0.1`, and `scripts/regen_mcp_json.py --check` passed.
- Unknown future Google tools now fail closed: `config/tools.yaml:1128` uses a Google wildcard with `access_mode: write`, and `tools/gatekeeper_can_use_tool.py:168` denies wildcard write/destructive tools without explicit entries.
- Google read output wrapping is registry-driven: `config/tools.yaml:1134` contributes `^mcp__google_workspace__`, and `agents/external_wrap_hook.py:149` reads wrap patterns from the registry when engagement config does not override them.
- Auth precheck is no longer shadow by default: `config/engagement.yaml:871` sets `enforce`, `agents/hooks.py:676` honors the config after env overrides, and the targeted auth tests passed.
- Calendar daily check-in no longer depends on a prompt-mediated YAML fetch: `agents/daily_checkin.py:358` uses the typed adapter, and `tools/calendar/get_events.py:103` performs the direct MCP call.

## 4. New regressions or contradictions

- The prompt-level claim that drafts, calendar adds, and Drive uploads auto-run contradicts the registry’s gatekeeper entries.
- The prompt-level package note says `google-workspace-mcp 1.27+`, while the actual configured version is `2.0.1`.
- Startup comments around the Google probe say the scheduler already reads `calendar_heartbeat_healthy`, but the scheduler is constructed before the fresh probe populates it.
- The fabrication backstop’s calendar-fetch allowlist is still keyed to raw Google Workspace tool names and misses the newer in-process typed adapter.

## 5. Missing tests / suggested verification

- Add approval-preview tests that assert Google write/draft/calendar/upload/document/sheet/slide approvals include all critical fields or refuse over-large critical payloads. Existing `tests/test_approval_preview_truthful.py:117` only checks that the per-tool Gmail-send summarizer returns a string.
- Add a post-filter test proving `mcp__hikari_utility__calendar_get_events` satisfies the calendar fabrication guard.
- Add scheduler startup-order tests around stale `runtime_state.calendar_heartbeat_healthy` values and fresh probe results.
- Expand `tests/test_subagent_prompt_policy_drift.py` so it derives all Google Workspace `gatekeeper` entries from `config/tools.yaml` instead of maintaining the narrow hand list at `tests/test_subagent_prompt_policy_drift.py:37`.
- Consider a typed Gmail adapter for daily check-in, or at least test that `agents/daily_checkin.py:291` cannot route through write-capable subagent behavior when it only intends read queries.
- Run `scripts/validate_mcp_servers.py` in an environment with real Google credentials and network access before release. I did not run live MCP introspection in this pass; the local registry and generated config checks were clean.

## 6. Sprint or roadmap implications

The highest-value next sprint item is tightening consent truthfulness: make the gatekeeper approval prompt show what will actually be sent, created, uploaded, or mutated. That is the main safety gap left after the registry/gating work.

Second, close the adapter migration edges: teach the post-filter about the typed calendar adapter and decide whether Gmail daily check-in should also become a typed adapter instead of a prompt/subagent YAML path.

Third, reduce policy drift by generating subagent tool-surface text from the registry or by keeping prompts policy-neutral. The registry is now the trustworthy layer; the docs should stop becoming a parallel source of truth.

Finally, make startup health state monotonic within a boot: probe Google credentials before scheduling Google-dependent jobs, or schedule the job unconditionally and let it self-disable when current health is bad.

## 7. Sources used

Local source, tests, and config inspected directly in this working tree:

- `config/tools.yaml`
- `.mcp.json`
- `tools/gatekeeper.py`
- `tools/gatekeeper_can_use_tool.py`
- `tools/_tools_yaml.py`
- `agents/hooks.py`
- `agents/telegram_bridge.py`
- `agents/scheduler.py`
- `agents/post_filter.py`
- `agents/daily_checkin.py`
- `tools/calendar/get_events.py`
- `auth/google.py`
- `auth/scope_match.py`
- `agents/subagents/prompts/drive_gmail.prompt.md`
- `agents/subagents/prompts/drive_gmail.description.md`
- Targeted tests listed in section 1.

Official external sources checked for Google API/scope behavior:

- [Google Gmail API scopes](https://developers.google.com/workspace/gmail/api/auth/scopes) — Gmail scope capabilities and restricted/sensitive scope meanings.
- [Google Calendar API scopes](https://developers.google.com/workspace/calendar/api/auth) — Calendar scope capabilities, including broad `calendar` and narrower event scopes.
- [Google Drive API scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth) — Drive scope capabilities and recommendation to request narrow scopes where possible.
- [Google OAuth 2.0 refresh token expiration](https://developers.google.com/identity/protocols/oauth2#expiration) — refresh tokens can stop working due to revocation, inactivity, password changes with Gmail scopes, testing-mode expiry, admin policy, and token limits.
