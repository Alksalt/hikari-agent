---
title: Second-Pass Tool Policy and Security Boundary Review
date: 2026-05-24
tags:
  - codex
  - second-pass
  - security
  - tools
  - mcp
  - permissions
  - approvals
---

# Second-Pass Tool Policy and Security Boundary Review

Domain: Tool Policy / Security Boundary

Scope: current working tree as inspected on 2026-05-24. Existing `codex/*.md`
files were treated as prior context/checklists only, not as truth. I did not
edit production code or tests.

## 1. Current-State Summary

The current tool-policy posture is substantially fail-closed around unknown and
future write surfaces. The registry is explicit, `gate: defer` is gone, most
Google Workspace / Notion / GitHub writes are gatekeeper-gated, generic
filesystem access is absent from the runtime allowlist, attachment reading is
path-contained, external MCP is read-only plus authenticated, untrusted output
wrapping is installed, and `python_run` is both gatekeeper-gated and sandboxed.

Verification run:

- `uv run python -m pytest -q tests/test_tools_yaml.py tests/test_tool_policy_access_mode.py tests/test_destructive_tool_gating.py tests/test_approval_preview_truthful.py tests/test_mcp_external.py tests/test_mcp_external_oauth.py`
- Result: `143 passed, 1 warning in 4.51s`
- `uv run python scripts/validate_tool_registry.py`
- Result: `validate_tool_registry: clean.`
- `uv run python scripts/validate_mcp_servers.py --skip apple_events,apple_shortcuts`
- Result: Google Workspace, Notion, and YouTube transcript coverage clean; duckdb,
  github, and playwright skipped because those MCP servers did not respond to
  initialize in this environment.

The remaining risk is concentrated in the human approval boundary rather than
the raw allow/deny policy: some gatekeeper previews do not show the exact
payload that will be sent or written. There are also intentional-but-risky
ungated local Apple Events write tools, OAuth client-registration UX gaps, and
prompt/test drift that can mislead maintainers about what is gated.

P0: no P0 found in this pass.

## 2. Findings, Ordered P0/P1/P2/P3

### P1 - Gatekeeper approval previews can hide material write payloads

Status: still open in current source.

Evidence:

- `tools/gatekeeper_can_use_tool.py:104-119` lets a per-tool summarizer win
  outright; the generic truthful fallback only runs when no summarizer exists.
- `tools/gatekeeper_can_use_tool.py:183-185` uses that summary as the approval
  preview.
- `tools/gatekeeper.py:287-295` summarizes Gmail send/reply by showing only
  recipients plus a truncated subject, or a message id plus an 80-character
  body preview. Full body, HTML body, cc, bcc, attachments, and other fields are
  not surfaced.
- `tools/gatekeeper.py:306-317` summarizes calendar and Drive writes without
  full location, attendee, content, or path details.
- `tools/gatekeeper.py:319-337` summarizes Notion writes by id/title-like fields,
  not full content/properties.
- `tools/gatekeeper.py:339-347` summarizes GitHub issue/PR creation with a
  truncated title and omits the body/head/base/review details.
- `tools/gatekeeper.py:363-365` summarizes dispatch with an 80-character task
  preview and omits fields such as `repo_path`, `allowed_tools`, and `max_turns`.
- `tests/test_approval_preview_truthful.py:117-126` only asserts that these
  custom summarizers return a string. It does not require them to include all
  material fields.

Impact:

The owner can approve a gated action without seeing the actual data that will be
sent, uploaded, written, or delegated. For example, `CONFIRM-SEND` can be based
on an email preview that omits most of the message payload, or a dispatch
approval can omit the target repository and tool budget. That weakens the core
human approval boundary even when the tool is technically gated.

Recommendation:

Make custom summaries a headline only, then append the generic critical-field
preview for all gated tools. Alternatively, require each summarizer to declare
which critical fields it fully exposes and fail tests if any remain hidden. Add
tests for Gmail `body` / `html_body` / `cc` / `bcc`, dispatch `repo_path` and
`allowed_tools`, Notion content/properties, GitHub body/base/head, calendar
attendees/location, and Drive upload paths/content.

### P2 - Apple Events write/delete tools remain intentionally ungated

Status: still open as an accepted-design risk unless the project wants a
stricter LLM boundary.

Evidence:

- `config/tools.yaml:1741-1784` explicitly registers Apple Events reminder and
  calendar create/delete tools as `access_mode: write` with `gate: null`.
- `config/tools.yaml:1786-1795` sets the Apple Events wildcard to write, but
  the explicit entries bypass wildcard fail-closed behavior.
- `tests/test_destructive_tool_gating.py:90-110` asserts these Apple Events
  writes must not be gatekeeper-gated and documents the rationale as local,
  reversible, and low-risk.

Impact:

A prompt-injection-steered model can create or delete local/iCloud reminders and
calendar events without owner confirmation. That may be acceptable for trusted
internal scheduler flows, but exposing the same ungated writes to normal LLM tool
selection leaves a real policy exception.

Recommendation:

Split scheduler-internal Apple write adapters from LLM-facing tools. Keep the
internal reminder/calendar mirroring path direct if needed, but gate or narrow
the LLM-facing create/delete tools. If this remains an intentional exception,
track it as an accepted risk with explicit abuse scenarios and recovery steps.

### P2 - External OAuth client authorization is weakly inspectable and phishable

Status: open.

Evidence:

- `mcp_external/oauth.py:151-169` accepts any `http` or `https` redirect URI with
  a network location and no embedded credentials. It does not restrict cleartext
  HTTP to loopback hosts.
- `mcp_external/oauth.py:227-260` supports dynamic client registration for
  arbitrary valid redirect URIs.
- `mcp_external/oauth.py:295-304` renders the authorization form with mutable
  `client_name` and scope, but does not show `redirect_uri`, `client_id`, client
  origin, or trust status.
- `mcp_external/oauth.py:471-473` redirects after a passphrase POST with HTTP
  302 instead of the unambiguous POST-to-GET 303 recommended for credential
  submission redirects.

Impact:

An attacker can register a client named like a trusted integration and direct
the owner through an authorization page that does not display the destination
redirect URI. If the redirect is non-loopback cleartext HTTP, authorization
codes may traverse the network. The 302 after credential submission is common,
but less explicit than 303 for preventing credential-forwarding ambiguity.

Recommendation:

Show `client_id`, full `redirect_uri`, scheme/host, and trust status on the
authorization page. Restrict `http://` redirects to loopback IP literals unless
there is a deliberate development-mode exception. Prefer 303 after passphrase
POST. Consider requiring an initial access token or a local trust store for DCR
outside development.

Primary sources checked:

- RFC 7591 says the dynamic registration endpoint may accept unauthenticated
  registration, but also discusses initial access tokens and rate limiting.
- RFC 8252 treats cleartext HTTP loopback redirects as acceptable because the
  request never leaves the device, and recommends loopback IP literals.
- RFC 9700 states that authorization servers should automatically redirect only
  to trusted redirect URIs and should use 303 after credential POST redirects
  when using HTTP redirection.

### P2 - Google Workspace prompt/test policy drift contradicts runtime gating

Status: open documentation/test drift; runtime gating is stronger than the docs.

Evidence:

- `agents/subagents/prompts/drive_gmail.prompt.md:1` says the subagent runs with
  `permission_mode=acceptEdits` and that drafts, calendar adds, and Drive uploads
  auto-run.
- `agents/subagents/prompts/drive_gmail.description.md:1` repeats the same
  auto-run claim.
- Current `config/tools.yaml` gates the relevant write tools, including calendar
  create around `config/tools.yaml:650-656`, Drive upload and Gmail draft create
  around `config/tools.yaml:669-689`, Gmail send/delete around
  `config/tools.yaml:691-711`, Docs writes around `config/tools.yaml:724-788`,
  and Sheets writes around `config/tools.yaml:790-830`.
- `tests/test_subagent_prompt_policy_drift.py:37-46` checks only a narrow older
  list of bare destructive tools and misses newer surfaces such as drafts,
  calendar adds, uploads, Docs, Sheets, and Slides.

Impact:

The runtime appears safer than the prompt text suggests, but the stale prompt
can mislead maintainers, reviewers, and subagent behavior about when user
approval is expected. The drift test is too narrow to catch future contradictions
as the Google Workspace surface grows.

Recommendation:

Generate the subagent write-policy snippet from `config/tools.yaml`, or update
the prompt and add a drift test over all `access_mode != read` Google Workspace
tools. The test should fail if any prompt says a gatekeeper-gated write
"auto-runs".

### P3 - Playwright is advertised to research but denied by wildcard-write policy

Status: open contradiction, secure fail-closed.

Evidence:

- `config/tools.yaml:1806-1813` registers `mcp__playwright__*` as wildcard write
  with `gate: null`.
- `tools/gatekeeper_can_use_tool.py:168-178` denies any wildcard write or
  destructive tool without an explicit gatekeeper decision.
- `config/tools.yaml:1868-1874` includes `mcp__playwright__*` in the research
  subagent tool list.
- `agents/subagents/prompts/research.prompt.md:1-3` tells the research subagent
  to use Playwright as a last resort.

Impact:

The policy fails closed, which is good, but the advertised capability is not
actually usable. JS-rendered research pages may fail mid-task rather than cleanly
falling back to a supported path.

Recommendation:

Either enumerate specific safe Playwright read/snapshot/navigate tools and gate
state-changing actions, or remove Playwright from the research subagent prompt
and registry until a narrower policy exists.

### P3 - Cockpit auth-precheck status can disagree with runtime enforcement

Status: open observability bug.

Evidence:

- `config/engagement.yaml:871-872` sets `auth.precheck: enforce`.
- `agents/hooks.py:676-731` reads env override first, then config, then defaults
  to shadow.
- `agents/cockpit.py:78-80` reads only env vars and otherwise returns `shadow`.

Impact:

Runtime enforcement can be active while the cockpit reports shadow mode. This
does not appear to weaken enforcement, but it can mislead the operator during an
incident or rollout check.

Recommendation:

Make cockpit read the same config fallback as `agents/hooks.py`, or centralize
auth-precheck mode resolution in one helper used by both.

## 3. Previously Reported Issues That Now Look Closed

No prior `codex/*.md` report was treated as authoritative. From current source,
these areas look closed or materially improved:

- Generic filesystem tools are absent from the runtime allowlist. `tests/test_smoke.py:263-276`
  asserts `Read`, `Glob`, and `Grep` are absent and `read_attachment` is present.
- Attachment reading is hard-scoped. `tools/attachments/read.py:20-26`,
  `tools/attachments/read.py:47-60`, and `tools/attachments/read.py:69-82`
  enforce allowed roots, regular files, and size limits.
- Unknown tools and future write/destructive wildcards fail closed.
  `tools/gatekeeper_can_use_tool.py:160-178` implements this, and
  `tests/test_tool_policy_access_mode.py:98-135` covers it.
- Google Workspace, Notion, and GitHub write surfaces are mostly explicit and
  gatekeeper-gated. Coverage is backed by `tests/test_tools_yaml.py:126-151`,
  `tests/test_destructive_tool_gating.py:23-80`, and
  `tests/test_destructive_tool_gating.py:146-175`.
- The old defer path is gone from the registry. `tests/test_tools_yaml.py:126-130`
  asserts there are no `gate: defer` entries.
- The hook path has been narrowed to scope precheck rather than a broad defer
  prompt. See `agents/hooks.py:741-760`.
- Untrusted output wrapping is installed for tool results. `agents/runtime.py:315-324`
  wires the hook, and `agents/external_wrap_hook.py:72-115` plus
  `agents/external_wrap_hook.py:156-187` wrap list content, flat string content,
  and bare string outputs.
- External MCP is read-only, requires authentication, and has localhost/tunnel
  hardening. See `mcp_external/launch.py:225-240`,
  `mcp_external/server.py:132-160`, and `mcp_external/server.py:162-242`.
- `python_run` is gated and sandboxed on macOS. See `config/tools.yaml:472-477`
  and `tools/calc/python_run.py:61-118`.
- Auth precheck is configured to enforce in runtime config and read by hooks.
  See `config/engagement.yaml:871-872` and `agents/hooks.py:676-731`.
- Link shelf URL fetching appears SSRF-hardened. `tools/link_shelf/_safe_fetch.py`
  pre-resolves and blocks private/loopback/link-local/cloud metadata ranges,
  validates each redirect hop, handles IPv6 transition forms, and caps redirects.

## 4. New Regressions or Contradictions

- The drive/gmail subagent prompt says several Google writes auto-run, while the
  registry now gates them. This is a contradiction in the docs/prompts, not a
  runtime bypass.
- The research subagent advertises Playwright, while wildcard-write policy denies
  it. This is a secure operational contradiction.
- Cockpit can display auth precheck as `shadow` while runtime config enforces it.
- A new second-pass Telegram UX report and index edits already existed in the
  working tree before this report was written. I did not inspect those as truth
  for this domain and did not modify them.

## 5. Missing Tests / Suggested Verification

- Add approval-preview tests that assert every gated tool exposes material
  payload fields, not merely that a summary string exists.
- Add per-domain approval tests for Gmail, Calendar, Drive, Docs, Sheets, Slides,
  Notion, GitHub, dispatch, and Python execution.
- Add a drift test that compares subagent prompt claims against all
  `access_mode != read` and `gate: gatekeeper` entries in `config/tools.yaml`.
- Add a cockpit/runtime parity test for auth-precheck mode resolution.
- Add OAuth tests for rejecting non-loopback `http://` redirect URIs, displaying
  redirect URI/client id on the authorize page, and returning 303 after
  passphrase POST.
- Add a policy test that either proves safe explicit Playwright read tools exist
  or proves the research subagent does not advertise Playwright.
- If Apple Events remains ungated, add an explicit accepted-risk test/comment
  that distinguishes internal scheduler calls from LLM-facing writes.

## 6. Sprint or Roadmap Implications

Suggested sprint order:

1. Approval preview hardening. This is the highest-leverage security boundary
   fix because it preserves the existing gatekeeper model while making approvals
   materially truthful.
2. Google prompt/test drift cleanup. Low implementation cost, prevents future
   regressions in a large and risky tool surface.
3. OAuth authorization UX and redirect validation. Important before exposing
   external MCP beyond tightly controlled local use.
4. Apple Events boundary split. Decide whether ungated local writes are a
   product feature or an accepted risk; then encode that decision in policy and
   tests.
5. Playwright policy cleanup. Either support a narrow safe browser surface or
   remove the advertised capability.
6. Observability parity. Fix cockpit auth-precheck mode reporting so operators
   can trust rollout status.

## 7. Sources Used

Local source, config, docs, and tests:

- `config/tools.yaml`
- `config/engagement.yaml`
- `.mcp.json`
- `tools/gatekeeper.py`
- `tools/gatekeeper_can_use_tool.py`
- `tools/attachments/read.py`
- `tools/calc/python_run.py`
- `tools/link_shelf/_safe_fetch.py`
- `tools/link_shelf/handlers.py`
- `agents/hooks.py`
- `agents/runtime.py`
- `agents/external_wrap_hook.py`
- `agents/cockpit.py`
- `agents/subagents/prompts/drive_gmail.prompt.md`
- `agents/subagents/prompts/drive_gmail.description.md`
- `agents/subagents/prompts/research.prompt.md`
- `mcp_external/oauth.py`
- `mcp_external/launch.py`
- `mcp_external/server.py`
- `tests/test_tools_yaml.py`
- `tests/test_tool_policy_access_mode.py`
- `tests/test_destructive_tool_gating.py`
- `tests/test_approval_preview_truthful.py`
- `tests/test_mcp_external.py`
- `tests/test_mcp_external_oauth.py`
- `tests/test_smoke.py`
- `tests/test_subagent_prompt_policy_drift.py`

External primary sources:

- [RFC 7591: OAuth 2.0 Dynamic Client Registration Protocol](https://www.rfc-editor.org/rfc/rfc7591.html)
- [RFC 8252: OAuth 2.0 for Native Apps](https://www.rfc-editor.org/rfc/rfc8252.html)
- [RFC 9700: Best Current Practice for OAuth 2.0 Security](https://www.rfc-editor.org/rfc/rfc9700.html)

