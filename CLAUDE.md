# Hikari Agent — dev env

**Hikari's persona lives at `assets/PERSONA.md`** — loaded as the SDK system prompt by `agents/runtime.py:_persona()`. That is the only file the bot loads as its constitution. Do not paste persona content here.

**Subagent / tool / skill index** — see `AGENTS.md`.
**Cheap aux LLM picks (verified)** — see `MODELS.md`.

---

## project rule — cost-aware LLM/embedding routing

**Main path** (Hikari turns, drift judge, anything per-turn): `CLAUDE_CODE_OAUTH_TOKEN` via `claude-agent-sdk` only. Never set `ANTHROPIC_API_KEY` — the SDK falls back to it and double-bills on top of the $200/mo Max subscription.

**Aux LLM work — SDK is the default** (migrated 2026-06-10): use `agents.runtime.run_internal_text(prompt, *, system, model, max_tokens)` — stateless single-shot, no tools, no persona, Haiku by default (`MODEL_HAIKU`; pass `model=MODEL_PRIMARY` for large structured output or voice pieces). Returns `""` on failure; cost lands under `path="aux_sdk"`. For control calls that DO need SDK tools, use `run_internal_control(prompt, *, max_turns, max_budget_usd, extra_allowed_tools=None)` (Sonnet, tool-capable).

**OpenRouter (`_call_aux_llm`) — kept ONLY for synchronous pre-reply classifiers** where the ~3-6s SDK subprocess spawn would degrade chat latency. Today: sticker selection (`agents/stickers.py`) and the dispatch task extractor (`tools/dispatch/task_extractor.py`). New aux work defaults to `run_internal_text`; add OpenRouter work only for the same latency reason, using a current cheap model from `MODELS.md` (verified 2026-05-23). Default `deepseek/deepseek-v4-flash` ($0.14/$0.28, 1M ctx, 384K out). Fallback chain: `mistralai/mistral-small-2603` → `google/gemini-2.5-flash-lite` → `z-ai/glm-4.7-flash`. **Do NOT use `deepseek/deepseek-chat`** — alias retires 2026-07-24. Set `OPENROUTER_API_KEY`.

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
  - uv run python scripts/validate_mcp_servers.py --skip apple_events --allow-unreachable playwright
wiki_path: /Users/ol/Library/Mobile Documents/iCloud~md~obsidian/Documents/alt-wiki
```
