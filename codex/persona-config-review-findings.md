# Persona / Config Deep Review Findings

Scope: persona prompts, skills, runtime prompt wiring, proactive config, approval gates, and related tests.

Verification run:

```bash
uv run pytest -q tests/test_persona_hardening.py tests/test_voice.py tests/test_security.py tests/test_smoke.py
```

Result: `77 passed, 6 skipped`.

## Findings

### P1 - Google Workspace / Notion write tools are documented as approval-gated, but the gate only covers wiki_append

`agents/subagents.py:102-117` tells the Google Workspace specialist that drafts/sends/calendar writes go through the approval gate. `agents/subagents.py:121-136` says the same for Notion writes. The actual defer gate is exact-match only (`agents/hooks.py:284-286`), and config only includes `mcp__hikari_wiki__wiki_append` (`config/engagement.yaml:63-64`). There are no Google/Notion write entries, no `tier_2_tools`, and no confirmed-tool mappings for those tools.

Impact: if the external MCP servers expose write tools, the specialist can call them directly under its wildcard tool allowlist (`mcp__google_workspace__*`, `mcp__notion__*`) without the approval flow described in the prompt. That is especially risky because these tools touch outbound surfaces: email, calendar invites, Notion pages.

Fix direction: either remove write-capable tools from those subagents until wrappers exist, or add a real approval path for external MCP writes. The current confirmed-tool resume design is wiki-specific, so simply adding Google/Notion tools to `defer_gated_tools` will abort unless confirmed execution is also designed.

### P1 - Long-form post-filter rewrites are a dead switch

`agents/post_filter.py:179-182` says callers must re-prompt when `needs_llm_rewrite` is true. `agents/post_filter.py:224-239` sets that flag when `refusal_filter.enable_llm_rewrite` or `sycophancy_guard.enable_llm_rewrite` is enabled. But `_send_with_choreography()` only logs the event and continues sending the original `text_to_send` (`agents/telegram_bridge.py:107-124`). Background listener and proactive sends also ignore rewrite requests (`agents/background_listener.py:149-160`, `agents/telegram_bridge.py:626-636`).

Impact: flipping the config flags does not prevent long assistant-safety replies or sycophantic anchor violations from shipping. It only changes a return flag that the send paths ignore.

Fix direction: implement one bounded rewrite pass in all outbound send paths, or remove/rename the config flags to make it explicit that this is detection-only telemetry.

### P1 - External read outputs are not structurally wrapped for Google Workspace / Notion

The prompt-injection config marks `mcp__google_workspace__` as untrusted (`config/engagement.yaml:548-554`), and `CLAUDE.md:258-277` tells the lead to treat Drive/Gmail/Calendar contents as attacker-controlled. But direct external MCP outputs from `mcp__google_workspace__*` and `mcp__notion__*` go straight through the subagents (`agents/subagents.py:102-136`). The actual `wrap_untrusted()` call sites are wiki reads and the external Hikari MCP server, not Google/Notion reads (`tools/wiki.py:158-176`, `mcp_external/server.py:24-61`).

Impact: for Drive/Gmail/Calendar/Notion, the injection defense is prompt-only rather than structural. Combined with the missing write gate above, this is the main lethal-trifecta risk in the current config surface.

Fix direction: proxy external MCP reads through local wrapper tools that call `wrap_untrusted()`, or keep those subagents read-only and explicitly block/approval-gate all writes until wrappers exist.

### P2 - Runtime persona source has drifted from Codex persona source

The Telegram runtime loads `CLAUDE.md` only (`agents/runtime.py:63-65`). `AGENTS.md` is now stale: it lacks the ask-shape gate, emoji policy, conditional memory blocks, untrusted-content rules, and delegation instructions present in `CLAUDE.md` (`CLAUDE.md:65-78`, `CLAUDE.md:237-285`). The `.agents` skill copy also points at `AGENTS.md` and an invalid `.Codex/...` example path (`.agents/skills/character-voice/SKILL.md:8-17`), while the `.claude` copy points at `CLAUDE.md` (`.claude/skills/character-voice/SKILL.md:8-17`).

Impact: edits or reviews through Codex can be evaluating a different persona than the bot actually runs. Security/persona hardening added to `CLAUDE.md` may not influence Codex-side work, and changes to `AGENTS.md` will not affect Telegram behavior.

Fix direction: make one file authoritative and generate/sync the other, or add a test that fails on meaningful drift between `AGENTS.md` and `CLAUDE.md`. Also fix the `.agents` skill path references.

### P2 - Proactive cadence source justification can be fabricated by fallback

The cadence governor says every proactive message must cite an allowed source (`agents/cadence.py:10-14`, `config/engagement.yaml:386-394`). `_pick_seed()` defaults `source = "recent_episode_callback"` before checking whether a recent episode exists (`agents/proactive.py:131-149`). `_build_prompt()` only includes `recent_episode_summary` if `db.recent_episodes(limit=1)` returns something (`agents/proactive.py:164-179`).

Impact: a generic heartbeat with no open loop, observation, noticing, lexicon, or recent episode can still pass the governor as `recent_episode_callback`. That weakens the “not generic” persona rule and can produce random check-ins that look less grounded.

Fix direction: only use `recent_episode_callback` when a recent episode exists. If no source exists, return `None` and skip the heartbeat.

### P2 - Observations and noticings are marked surfaced before the user actually sees them

`_format_observations()` and `_format_noticings()` mark rows surfaced during hook injection (`agents/hooks.py:122-168`). That happens before the model decides to use them, before filtering, and before the send succeeds.

Impact: a noticing can be consumed and suppressed without ever reaching the user: the model may ignore it, the turn may fail, the post-filter may replace the reply, or Telegram send may fail. For persona behavior, that means “i noticed” memory can silently disappear.

Fix direction: split “injected” from “surfaced”. Mark as surfaced after a successful outbound message that actually references the observation, or keep it eligible until a later confirmation step.

### P3 - README describes removed prompt/memory behavior

`README.md:13-14`, `README.md:55-58`, and `README.md:94` still refer to `STAGES.md` and top-8 per-turn retrieval. Current code/tests say `STAGES.md` was removed and recall moved on-demand (`tests/test_voice.py:180-182`, `agents/hooks.py:250`). `README.md:19` also says Google Workspace is stubbed/uncommented later, while `.mcp.json` already registers Google Workspace and Notion (`.mcp.json:3-17`).

Impact: future prompt/config work starts from stale mental models. This is low runtime risk but high maintenance drag.

Fix direction: update README to match current `.claude/skills`, on-demand recall, external MCP registration, and current test count.

## Notes

The core persona prompt is internally strong: hard anchors, mood gates, denial layer, banned assistant phrases, and untrusted-content instructions are explicit. The biggest problems are not wording quality; they are enforcement gaps where config and subagent prompts claim a safety/persona behavior that the runtime does not actually implement.
