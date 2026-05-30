# Hikari Agent — dev env

**Hikari's persona lives at `assets/PERSONA.md`** — loaded as the SDK system prompt by `agents/runtime.py:_persona()`. That is the only file the bot loads as its constitution. Do not paste persona content here.

**Subagent / tool / skill index** — see `AGENTS.md`.
**Cheap aux LLM picks (verified)** — see `MODELS.md`.

---

## project rule — cost-aware LLM/embedding routing

**Main path** (Hikari turns, drift judge, anything per-turn): `CLAUDE_CODE_OAUTH_TOKEN` via `claude-agent-sdk` only. Never set `ANTHROPIC_API_KEY` — the SDK falls back to it and double-bills on top of the $200/mo Max subscription.

**New aux LLM work — prefer SDK** (per DECISIONS 2026-05-28): use `agents.runtime.run_internal_control(prompt, *, max_turns, max_budget_usd, extra_allowed_tools=None)`. Stateless, no session resume, Sonnet (subscription), tool-capable. Use this for any new control / classifier / summariser call.

**Existing aux ops on OpenRouter — kept on OpenRouter for now**: `run_aux_composition` + `run_reflection_call` + `_call_aux_llm` and their ~9 call sites (reflection, diary, tonal_recall, dialectic, drift_judge correction). Migration to `run_internal_control` is a follow-up sprint — do not migrate ad hoc. When you add new OpenRouter work despite the SDK preference, use a current cheap model from `MODELS.md` (verified 2026-05-23). Default `deepseek/deepseek-v4-flash` ($0.14/$0.28, 1M ctx, 384K out). Fallback chain: `mistralai/mistral-small-2603` → `google/gemini-2.5-flash-lite` → `z-ai/glm-4.7-flash`. **Do NOT use `deepseek/deepseek-chat`** — alias retires 2026-07-24. Set `OPENROUTER_API_KEY`.

**Embeddings**: hosted API is fine (`text-embedding-3-small` via `OPENAI_API_KEY` ≈ $0.02/1M tokens — basically free at Hikari volume). Local `fastembed` (`tools/embeddings.py`, `BAAI/bge-small-en-v1.5`, 384-dim) is also fine. Either works.

**Forbidden**: Anthropic models on OpenRouter (priced like the direct API), OpenAI chat completions (use OpenRouter+DeepSeek instead), any LLM costing >$1/1M tokens without flagging the cost first.

**STT**: OpenAI Whisper API (`voice.transcription_provider: openai_whisper_api`, `OPENAI_API_KEY`). Local faster-whisper removed 2026-05-30 — it didn't work reliably. Missing key raises loudly at call time.

## Ship profile

```yaml
base_branch: main
ship_method: push
quality_gates:
  - uv run pytest -q
  - uv run python scripts/validate_tool_registry.py
  - uv run python scripts/validate_mcp_servers.py --skip apple_events,apple_shortcuts --allow-unreachable duckdb,github,playwright
wiki_path: /Users/ol/Library/Mobile Documents/iCloud~md~obsidian/Documents/alt-wiki
```
