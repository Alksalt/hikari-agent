# Cheap OpenRouter Models — Memory Extraction & Classification

**Budget:** ≤ $1.50 per 1M tokens (in or out)
**Use case:** Memory extraction, intent classification, structured output, RAG snippet ranking, lightweight agentic loops
**Verified:** May 23, 2026 via OpenRouter listings + community sources

> Output price matters more than input price for classifiers (short outputs) but matters less than you think for extraction (longer JSON outputs). Models below ranked by real-world fit for Meria-style workloads, not just headline price.

---

## TL;DR — Pick This

| Workload | Model | Why |
|---|---|---|
| Pure intent classifier (1-token outputs) | **GLM 4.7 Flash** or **Gemini 2.5 Flash Lite** | Cheapest in/out, fast TTFT |
| Memory extraction (JSON, multi-field) | **DeepSeek V4 Flash** | Best price/quality, 1M context, 384K output |
| Agentic tool-calling on a budget | **Grok 4.1 Fast** (current Meria primary) or **DeepSeek V4 Pro** | Tool calling that actually works |
| Open-weights backup / self-host plan | **Qwen3.5-35B-A3B** or **Mistral Small 4** | Apache-2.0, runs locally if needed |
| Don't pick | **DeepSeek V3.2** (V4 Flash supersedes), **Gemini 2.5 Flash** (output too expensive) | Outdated or overpriced for this tier |

---

## Top 10 Models

### 1. DeepSeek V4 Flash — `deepseek/deepseek-v4-flash`

- **Price:** $0.14 / $0.28 per 1M (OpenRouter lists $0.112 / $0.224 on cheapest provider, $0.10 / $0.20 elsewhere)
- **Cache hit:** $0.0028 / 1M input — 98% discount, dropped to 1/10 launch rate on 2026-04-26
- **Context:** 1M / Output: up to 384K
- **Released:** Apr 24, 2026
- **Architecture:** MoE, 284B total / 13B active, hybrid attention
- **Best for:** Default cheap workhorse. JSON extraction, classification, RAG ranking, summarization, agent loops where you don't need frontier reasoning.
- **Community verdict:** ✅ **Heavy adoption.** 3.39T weekly tokens on OpenRouter — the most used model in this tier. Replaced `deepseek-chat` and `deepseek-reasoner` aliases (those retire 2026-07-24). Reports of 150–200 tok/s. TokenMix and Felloai both call it the "default low-cost workhorse." Long-output advantage over Gemini Flash Lite (384K vs 65K cap) for long structured reports.
- **Watch out:** Slower TTFT than Gemini Flash Lite. Field reports note minor hits on Terminal Bench 2.0 multi-step tool traces vs V4 Pro.

### 2. Gemini 2.5 Flash Lite — `google/gemini-2.5-flash-lite`

- **Price:** $0.10 / $0.40 per 1M
- **Context:** 1M / Output: 64K
- **Released:** Jul 22, 2025 (stable GA), still actively recommended
- **Best for:** Classification, translation, summarization, fast-paced conversational AI. Multimodal (text + image + audio + video) as a free bonus. "Thinking" off by default for speed.
- **Community verdict:** ✅ **Production-proven classifier.** Google explicitly markets this for "live translation, summarising long documents, fast conversational AI." Currently your Meria classifier — no reason to change unless you want pure cost optimization, in which case go GLM 4.7 Flash. Still highly recommended in 2026 budget guides.
- **Watch out:** Output at $0.40 is 2× DeepSeek V4 Flash. For output-heavy extraction tasks, DeepSeek wins on price.

### 3. GLM 4.7 Flash — `z-ai/glm-4.7-flash`

- **Price:** $0.06 / $0.40 per 1M
- **Context:** 200K / Output: 16K
- **Architecture:** 30B-class SOTA
- **Best for:** Highest-volume classification where input is long and output is short (think: classify a 50K-token chunk into one of 20 categories). Cheapest input price in this entire list.
- **Community verdict:** ⚠️ **New and unproven at scale.** Z.ai's GLM 4.6 has strong community traction in agentic coding (Claude Code, Cline, Kilo Code communities praise it). 4.7 Flash is newer and the community hasn't fully spoken yet. Z.ai's reputation is solid — GLM 4.6 / 4.7 is a known good lineage. Try it as a Flash Lite alternative.
- **Watch out:** Only 16K output cap. Don't use for long JSON extraction.

### 4. Grok 4.1 Fast — `x-ai/grok-4.1-fast`

- **Price:** $0.20 / $0.50 per 1M
- **Context:** 2M / Output: no fixed cap
- **Released:** Nov 19, 2025
- **Best for:** xAI's self-described "best agentic tool calling model" — customer support, deep research, multi-turn tool loops. Reasoning toggle via `reasoning.enabled`.
- **Community verdict:** ✅ **Strong tool-calling rep at this tier.** Currently your Meria primary — and the consensus says it's earned that slot. 2M context is the longest in this list. Often free on OpenRouter during promo windows (xAI rotates free periods).
- **Watch out:** Slightly more expensive than DeepSeek V4 Flash for pure classification. The 2M context is overkill unless you actually need it.

### 5. DeepSeek V4 Pro — `deepseek/deepseek-v4-pro`

- **Price:** $0.435 / $0.87 per 1M — **now permanent** (announced 2026-05-22)
- **Cache hit:** $0.003625 / 1M input
- **Context:** 1M / Output: up to 384K
- **Architecture:** MoE, 1.6T total / 49B active
- **Best for:** Heavy reasoning, full-codebase analysis, multi-step automation, large-scale information synthesis. Reasoning efforts `high` and `xhigh` supported. SWE-bench Verified 80.6%.
- **Community verdict:** ✅✅ **Just got significantly more attractive.** DeepSeek confirmed on 2026-05-22 that the 75%-off "promo" is now the standing price — the previously crossed-out $1.74/$3.48 sticker is historical. The official docs now say: "The deepseek-v4-pro model API pricing will be officially adjusted to 1/4 of the original price after the 75% discount promotion ends on 2026/05/31 15:59 UTC." Translation: sale price becomes real price. This is the first open-weight model within striking distance of Claude Opus 4.7 and GPT-5.5 on real benchmarks, at ~1/30th the per-token cost. Codersera ranks it "best coding and reasoning model under $1/M tokens."
- **Watch out:** Still labelled a preview. Behavior may shift before GA — pin the version in production. For pure classification (1-token outputs) V4 Flash is still cheaper. V4 Pro earns its keep on reasoning-heavy extraction (e.g., "extract a structured profile from this 30-day onboarding conversation").

### 6. DeepSeek V3.2 — `deepseek/deepseek-v3.2`

- **Price:** $0.252 / $0.378 per 1M
- **Context:** 131K
- **Released:** Dec 1, 2025
- **Architecture:** DeepSeek Sparse Attention (DSA), agentic task synthesis pipeline
- **Best for:** Long-context reasoning with sparse attention efficiency. Tool-use settings. GPT-5-class reported performance, gold-medal IMO/IOI 2025.
- **Community verdict:** ⚠️ **Superseded by V4 Flash for most workloads.** V3.2 still solid but V4 Flash is cheaper, has longer context (1M vs 131K), and similar quality. Use V3.2 only if you have prompts already tuned to it. There's also a `v3.2-speciale` variant ($0.287/$0.431) optimized for max reasoning that's reported ahead of GPT-5 on hard reasoning.
- **Watch out:** Officially DeepSeek's old aliases (`deepseek-chat`, `deepseek-reasoner`) retire 2026-07-24. Don't build new infra on V3.2.

### 7. Gemini 3.1 Flash Lite — `google/gemini-3.1-flash-lite-preview` (or stable when released)

- **Price:** $0.25 / $1.50 per 1M
- **Context:** 1M / Output: 64K
- **Released:** Mar 3, 2026 (preview), GA model `google/gemini-3.1-flash-lite` listed May 7, 2026
- **Best for:** RAG snippet ranking, translation, data extraction, code completion — Google explicitly highlights these as improved over 2.5 Flash Lite. Full thinking-level support (minimal/low/medium/high).
- **Community verdict:** ⚠️ **Mixed.** Better quality than 2.5 Flash Lite per Google's own claims, and "approaches 2.5 Flash performance" — but 2.5× more expensive than 2.5 Flash Lite. If 2.5 Flash Lite works for you, stay there until 3.1 stabilizes more. Output at $1.50 is at the edge of budget — output-heavy workloads get expensive fast.
- **Watch out:** $1.50 output blows the budget on long JSON extraction. For Meria classifier (short outputs) it's fine.

### 8. Qwen3.5-35B-A3B — `qwen/qwen3.5-35b-a3b`

- **Price:** $0.139 / $1.00 per 1M
- **Context:** 262K
- **Architecture:** MoE, hybrid linear-attention + sparse MoE
- **Best for:** Open-weights alternative if you ever want to self-host on the Mac Mini M4 or migrate off OpenRouter. Comparable to Qwen3.5-27B.
- **Community verdict:** ✅ **Solid open-weights pick.** Alibaba's Qwen line is heavily used in the Chinese AI dev community and increasingly in Western projects. 10 providers on OpenRouter = high uptime. The A3B (active 3B) design means cheap inference if you self-host.
- **Watch out:** Output at $1.00 is high. Use for input-heavy, classification-style tasks. For extraction with long outputs, V4 Flash is cheaper.

### 9. Ministral 3 8B — `mistralai/ministral-8b` (or the 3B variant)

- **Price (8B):** $0.15 / $0.15 per 1M — **symmetric pricing**
- **Price (3B):** $0.10 / $0.10 per 1M
- **Context:** 128K
- **Released:** Dec 2, 2025
- **Best for:** Workloads where output cost matters as much as input — e.g., generating long structured JSON, multi-field memory extraction. Symmetric pricing means a 500-token-in / 2000-token-out extraction is cheaper here than DeepSeek V4 Flash's asymmetric pricing.
- **Community verdict:** ⚠️ **Niche but underrated.** Apache-2.0 (true open source). EU-based provider — relevant if you ever care about GDPR data residency for Norwegian/EU clients. Less buzz than DeepSeek but Mistral's small models have a quiet, steady following.
- **Watch out:** 128K context is shorter than the 1M-context options. 8B params means weaker reasoning than V4 Flash or Qwen3.5-35B.

### 10. Mistral Small 4 — `mistralai/mistral-small-2603`

- **Price:** $0.15 / $0.60 per 1M
- **Context:** 262K
- **Released:** Mar 16, 2026
- **Architecture:** Unifies Magistral (reasoning) + Pixtral (vision) + Devstral (coding) into one model
- **Best for:** Mixed workloads where you might need light vision (PDF screenshots for sales-offers-bot?) plus extraction in the same pipeline. Apache-2.0.
- **Community verdict:** ✅ **Growing traction** as Mistral's unified small model. Replaces the older Mistral Small 3.x line. Multimodal at this price tier is rare.
- **Watch out:** 24B-ish range, so weaker on hard reasoning than V4 Flash or V4 Pro. Use for medium-complexity work.

---

## Models I Did Not Include (And Why)

| Model | Price | Why skipped |
|---|---|---|
| Gemini 2.5 Flash | $0.30 / $2.50 | Output blows the budget for classifier/extraction. |
| Gemini 3 Flash Preview | $0.50 / $3.00 | Same problem, even worse. |
| Gemini 3.5 Flash | $1.50 / $9.00 | Marketed as cheap. Is not. |
| Grok 4.20 / 4.3 | $1.25 / $2.50 | Frontier reasoning, not a classifier. |
| GLM 4.6 | $0.43 / $1.74 | Output edge-of-budget; for coding agents, not extraction. |
| Mistral Medium 3.5 | $1.50 / $7.50 | Same problem as Gemini 3.5 Flash. |
| Llama 3.x | various | No 2026 Llama variants currently competitive on price/quality in this tier. |

---

## Recommendations for Meria Specifically

Given your stack (Grok 4.1 Fast primary + Gemini 2.5 Flash Lite classifier, Split-Brain Router, A/B testing DeepSeek V3.2):

1. **Replace V3.2 in your A/B with DeepSeek V4 Flash.** Same vendor, cheaper, longer context, fresher model. No reason to keep V3.2 in new infra.
2. **Add GLM 4.7 Flash as a third classifier candidate** for your A/B. $0.06 input is cheaper than Flash Lite ($0.10) — if classification quality holds, this is free money. Run it on 5% of classifier traffic for a week and compare F1.
3. **Keep Grok 4.1 Fast as primary.** No cheaper model beats it for agentic tool calling at this tier. The 2M context is also useful for full-thread context in 30-day onboarding flows.
4. **Add DeepSeek V4 Pro to your router as the "complex case" escalation target.** Now that $0.435/$0.87 is permanent (announced May 22), you no longer need to rush. V4 Flash handles 90% of Meria's workloads; V4 Pro takes the remaining 10% — onboarding-day-30 synthesis, observation extraction from week-long context, leader-promotion eligibility analysis — where reasoning depth actually matters. Same vendor, same context window (1M), same output cap (384K), just better reasoning when you need it.
5. **Memory extraction (Meria's daily summaries, observation extraction):** DeepSeek V4 Flash. Long output cap (384K) means single-shot extraction of full day chats without chunking.

---

## Sources

- OpenRouter model pages (deepseek, google, x-ai, qwen, mistralai, z-ai providers) — verified May 23, 2026
- DeepSeek official pricing page (verified 2026-05-23 — confirms V4 Pro permanent pricing)
- DeepSeek X announcement, 2026-05-22 (V4 Pro discount made permanent)
- Codersera "DeepSeek V4-Pro Review 2026" (updated 2026-05-23)
- Startup Fortune "DeepSeek making 75% discount permanent" (2026-05-22)
- Felloai DeepSeek Pricing 2026
- TokenMix.ai DeepSeek API Pricing 2026
- ofox.ai "Gemini 3.1 Flash Lite vs DeepSeek V4 Flash" budget agent showdown (May 2026)
- Artificial Analysis benchmarks (Intelligence Index v4.0)
- Hacker News thread on GLM 4.6 providers (Nov 2025)
- Mistral AI Mistral 3 announcement
