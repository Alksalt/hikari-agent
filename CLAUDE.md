# Hikari Agent — dev env

**Hikari's persona lives at `assets/PERSONA.md`** — loaded as the SDK system prompt by `agents/runtime.py:_persona()`. That is the only file the bot loads as its constitution. Do not paste persona content here.

**Subagent / tool / skill index** — see `AGENTS.md`.
**Cheap aux LLM picks (verified)** — see `MODELS.md`.

---

## project rule — cost-aware LLM/embedding routing

**Main path** (Hikari turns, drift judge, anything per-turn): `CLAUDE_CODE_OAUTH_TOKEN` via `claude-agent-sdk` only. Never set `ANTHROPIC_API_KEY` — the SDK falls back to it and double-bills on top of the $200/mo Max subscription.

**Cheap auxiliary LLM ops** (Graphiti entity extraction, summarizers, classifiers, occasional judges): use **OpenRouter** with a current cheap model — see `MODELS.md` (verified 2026-05-23) for the canonical list. Default `deepseek/deepseek-v4-flash` ($0.14/$0.28, 1M ctx, 384K out — memory-extraction workhorse). Fallback chain: `mistralai/mistral-small-2603` → `google/gemini-2.5-flash-lite` → `z-ai/glm-4.7-flash`. **Do NOT use `deepseek/deepseek-chat`** — that alias retires 2026-07-24. Set `OPENROUTER_API_KEY`. When MODELS.md is updated, prefer it over training-data memory.

**Embeddings**: hosted API is fine (`text-embedding-3-small` via `OPENAI_API_KEY` ≈ $0.02/1M tokens — basically free at Hikari volume). Local `fastembed` (`tools/embeddings.py`, `BAAI/bge-small-en-v1.5`, 384-dim) is also fine. Either works.

**Forbidden**: Anthropic models on OpenRouter (priced like the direct API), OpenAI chat completions (use OpenRouter+DeepSeek instead), any LLM costing >$1/1M tokens without flagging the cost first.

**STT**: default is local `faster-whisper` via `voice.transcription_provider: local_faster_whisper` (`tools/voice.py`). Config-switchable to `openai_whisper_api` as a fallback if the local model misbehaves.

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
