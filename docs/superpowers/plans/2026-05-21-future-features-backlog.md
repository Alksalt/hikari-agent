# Future Features Backlog (2026-05-21)

Captured from the 5-agent community research synthesis on 2026-05-21. The top
5 are scoped in `2026-05-21-top-5-roadmap.md`. Everything else lives here for
later picking. Order within each section is rough effort-ascending.

**Explicit exclusion (user, 2026-05-21):** Spotify MCP is OUT — do not add to
any plan or future iteration.

## Companion / character depth (from Character.AI / Replika / Nomi / SillyTavern scan)

- **Streak / gap awareness** — `S, ~2h`. Compute `now - last_message_ts`, inject `# gap_since_last: 4 days` into context. Her "you went quiet. that's disruptive." line already exists in CLAUDE.md; just needs the trigger plumbed. Short gap → invisible, week+ → noticing layer activates. Cross-listed in personal-life as "Drift mirror" but at finer time scale.

- **Per-day mood-thread continuity (yesterday's residue)** — `S-M, 1d`. Tag episodes with emotional weight, surface a "yesterday's residue" line in morning context. The CLAUDE.md emotional-half-life rule already permits this; just needs feeding as state. Already aligned with voice, low risk.

- **Edit-correction mechanism ("no, it was Tuesday")** — `M, ~1d`. Lightweight detector for "actually" / "no, it was" / "you're wrong about" → triggers a fact-edit flow. Maps perfectly onto her existing "yeah that was wrong. fixed." rule-5 line. Character.AI's most-requested 2025 feature. Should pair with the bi-temporal facts that already exist.

- **Multi-message bursts with typing latency** — `M, 2d`. Split-marker detection in her output → stage two sends with the Telegram typing indicator between them. Already permitted by CLAUDE.md "multi-message behavior" but runtime ships one block. Must stay rare (current "sometimes" cadence); never for warmth or compliments.

- **SillyTavern-style lorebook (keyword-triggered context overlay)** — `M, 2-3d`. Schema for keyed entries (user's recurring people / places / projects) + key matcher in inject_memory hook + system-prompt injector. Fires on keyword not similarity, so unusual entities ("nori the cat", "the kyiv apartment") always land. Invisible — voice-safe.

- **OOC channel ("/ooc" or "[ooc]" prefix)** — `S, ~3h`. Prefix detection + thin alternate system-prompt overlay that loosens deflection layer while keeping her dry register. The hard part is character integrity, NOT engineering. Risk: becomes the AI-assistant escape hatch she's forbidden from if voice slips even slightly. Defer until needed.

## Memory architecture (from 2025-2026 papers + Letta / Mem0 / Graphiti)

- **Actor-aware attribution on facts** — `S, ~2h`. Single column add: `facts.attribution` enum (`user_stated` / `user_observed` / `hikari_inferred` / `subagent_extracted` / `external_source`). Recall scorer ranks by attribution; contradiction resolution favors user_stated. Pure-additive, no architectural risk. Mem0 2026 pattern.

- **Per-category staleness curves with trust scores** — `S-M, 2d`. Add `fact_category` enum + `staleness_curves` table mapping category → half-life days. Retrieval score multiplies by `exp(-Δt / half_life)`. Trigger "still true?" canary when crossing 0.5 confidence. Mem0 2026 calls this the #1 unsolved failure (high-confidence stale facts). STALE paper arxiv 2605.06527.

- **Provenance / lineage column on every memory row** — `S to add, M to backfill writes`. `provenance(memory_id, table, parent_memory_id, parent_table, derivation_type)`. Every reflection / consolidation / evolution writes one row pointing at parents. Recall can downweight memories N hops from external sources. Composes with edge invalidation (#3 in top-5).

- **Memory admission control (don't write every observation)** — `M, 2-3d`. Pre-write classifier (rules + small LLM) scores candidates on `novelty × emotional_intensity × likely_recall`. Below threshold → drop into a 7-day shadow table that recall queries with low weight. Adaptive Memory Admission Control arxiv 2603.04549 showed 97.2% retention precision with 58% store reduction.

- **A-MEM "memory evolution"** — `M, 3d`. When a new episode/noticing is stored, retrieve neighbors → LLM pass may regenerate the neighbors' `summary` / `keywords` / `tags` and write bidirectional links. Preserves `summary_original`. NeurIPS 2025, arxiv 2502.12110, top-of-bench across 6 base models. Useful but adds an LLM-driven write path that could drift; needs an evolutions log.

- **Reflection tree with importance threshold** — `M, 3-4d`. Replace flat episode_summaries / weekly_consolidations_archive with a unified `reflections(level INT)` table. Trigger event-based ("when importance sum > θ") in addition to time-based. Generative Agents (Park et al. 2304.03442) + H²R arxiv 2509.12810 both showed event-triggered beats time-based on quality.

- **Procedural memory / skill library** — `M-L, 4-5d`. New `procedures(trigger_pattern, tool_sequence_json, success_count, fail_count)` table. Background sleep-time mines successful tool traces for repeated subsequences, proposes new procedures, surfaces them as fast-paths. Voyager-style + Letta context-repositories. Risk: locks in a bad pattern. Mitigate by requiring re-confirmation for first N uses.

## MCP integrations (excluding Spotify per user)

- **Readwise** — _in top-5 roadmap_. Listed here for cross-reference.

- **Apple Shortcuts MCP** — `S, ~1h install`. `recursechat/mcp-server-apple-shortcuts`. One-line bridge to the entire iOS automation ecosystem (Focus modes, HomeKit, Things, Drafts, Bear, Streaks, Day One) without writing a new MCP per app. Effort multiplier — adds capability for every Shortcut the user authors. Beta maturity. Top pick after Readwise.

- **Apple Health MCP** — `M, 2-3h`. `neiltron/apple-health-mcp` routes through DuckDB (already wired). Periodic XML export from iPhone. Turns the noticing rule from clever-sounding into actually-knowing — "you slept 4h three nights running. fix it or i'll stop pretending i don't notice." Fully local, zero recurring cost.

- **YouTube Transcript MCP** — `S, ~30min`. `jkawamoto/mcp-youtube-transcript`. Pulls transcripts with pagination (>50k chars). User drops a 90-min interview link → Hikari summarizes without him watching. No auth.

- **Feed MCP (RSS / Atom / JSON)** — `S, ~1h`. `richardwooding/feed-mcp`. Config file with feed URLs (HN, arxiv, Substacks, blogs). Personal information diet without algorithmic feed. User controls the list.

- **Home Assistant MCP** — `M if HA exists`. Official built-in (HA 2026.5+). Exposes entities as tools. "your bedroom's still 24°. i turned the AC down." Local network only.

- **Google Maps MCP** — `S, ~30min`. Routing + transit times complement her existing `places_search`. "if you leave in 12 minutes, you make it. later and you don't."

- **Strava MCP** — `M, 1-2h`. Activity feed, kudos. "you ran 6k yesterday and immediately ate two pizzas. interesting strategy." Logs to her `receipts` as `moved` band automatically.

- **Lunch Money MCP** (or Actual Budget if user prefers fully-local) — `M, 1-2h`. Transactions, categories. "you've spent ¥18k on convenience-store coffee this month. that's a flight."

- **Letterboxd MCP** — `M, 2h`. Read/write watchlist, ratings, diary. Hooks her *characteristic wrong opinions* directly into a real account ("you said *Annihilation* is overrated. it's not. logged it as 4.5 on your account."). Playwright-based, fragile (scraping).

- **n8n MCP server-trigger** — `L, requires self-host`. Trigger any workflow as a tool. Escape hatch for recurring multi-step automations not worth a dedicated MCP. Fully self-hosted = no third-party data flow.

## Claude SDK / agentic patterns (already-GA features Hikari isn't using)

- **Structured outputs (`output_config.format` / strict tools)** — `M, 1-2d`. GA in 2026 across Sonnet 4.5/4.6, Haiku 4.5. Reflection YAML and drift_canary verdicts go from "parse-or-fall-back-to-unknown" to schema-guaranteed. Removes ~100 lines of defensive parsing across reflection.py and drift_canary.py.

- **Extended/adaptive thinking on background jobs** — `S, ~2h`. `effort="medium"` on the drift judge + weekly reflection options. Adds a few cents/week in output tokens, materially improves judge labels. Chat path stays default (latency cost not worth it for 1-4 sentence replies).

- **Citations API for wiki/research subagents** — `M, 1-2d`. Wrap wiki page bodies in citation-enabled documents. Lets the bridge surface "source: alt-wiki/projects/foo". Supports the post_filter hallucination-detection work. Low-medium ROI; only worth it if a downstream check uses the citation spans.

- **`fork_session=True` for drift probes** — `S, ~2h`. SDK supports forking a session without mutating the original. Useful for counterfactual probes ("what would she say if asked X right now") and the belief_frame consolidation. Cheap; pair with periodic cleanup.

- **`SessionStore` protocol → SQLite-backed durable sessions** — `M, 2d`. Implement `SqliteSessionStore` colocated with `storage/db`. Cleaner session forensics, easier pruning, prepares for tag-based bucket organization (chat / proactive / reflection / drift). Zero token cost. Risk: a bug loses transcripts — gate behind a flag.

- **Context editing (`clear_tool_uses_at_least`)** — `S, ~1h`. Enable on the weekly reflection job only (walks long context). Prevents reflection from blowing the context window as the week grows. Don't enable on chat path.

- **Anthropic Memory tool** — `Skip for now`. Would duplicate Hikari's existing custom memory stack. Only consider for the `code_dispatch` coding subagents where you want a scratch memory across long-running implementation sessions without teaching them the custom recall layer.

## Personal-life automation (from QS / personal-AI scan)

- **Friction-engineering coach (Wendy Wood, not BJ Fogg)** — `M, 3d`. Reuse `receipts` table. Compute correlations between named environmental setups (mentioned in chat or receipts) and `avoided`/`moved`/`made` bands. Monthly job suggests one friction edit ("the weeks you left your phone in the kitchen you logged 'avoided' twice as often."). Descriptive, not prescriptive — Wood's research is unambiguous that environment beats motivation.

- **Prospective-intent ledger ("you said you would")** — `S-M, 2d`. Table `intents(statement, context, status, last_surfaced_ts)`. Captures stated future intent passively from chat ("i'll text marc back tomorrow") — not as a reminder. Surfaced sideways from recall when related topic recurs. Discipline: NEVER proactive, only on topic-return.

- **Energy-state router (not chronotype astrology)** — `M, 3-4d`. New `energy_state` core_block recomputed every few hours from message timestamps + tempo + receipt history + (optional) Apple Health sleep. Anchored to *this user's* observed rhythm, not generic owl/lark templates. Routes how she responds: heavy decision at depleted-state → push back ("this is a 9pm question. it'll be different at 10am.").

- **Relationship cadence ledger (Dunbar layer)** — `M, 3d`. Table `relations(person, ring, last_outbound_ts, baseline_interval_days)`. Inner ring only. Weekly check for `now - last_outbound > 2 * baseline` → one mention max per week. "you haven't talked to N in a while. is that on purpose." Voice must accept "yes, on purpose" and stop.

- **JITAI for one specific recurring failure mode** — `S for first one, L if generalized`. Config-driven `jitai.yaml` with one entry (trigger conditions, intervention message, cooldown, outcome capture). Pick ONE behavior the user repeatedly logs `avoided` for. JMIR 2025 walking study + Frontiers 2025 mental-health review both show personalized criteria outperform universal. Self-tunes via outcome rate.

- **Drift mirror: "what's changed about you this quarter"** — `S-M, 2d`. Reuses snapshot machinery. Quarterly job diffs current persona/preoccupation/lexicon snapshot vs 90-day-ago. Surfaces 3-5 specific shifts (vocabulary adopted/dropped, topics quieter, opinions flipped, people appeared/vanished). MUST include at least one regression or loss — symmetric. Adjacent to weekly consolidation and monthly Future-Self letter but at quarterly scale.

- **Quiet serendipity from the link shelf** — `S, ~1d`. Existing `links` table. Scoring view: `topic_similarity(current_episode, link_tags) × (age > 90d) × (times_referenced == 0)`. Surfaced sideways through `link_search` path. Capped to ~once per 10 days. Bayesian-surprise pattern.

- **Self-opacity tracker** — `M, 3d`. Table `unexplained_feelings(ts, statement, context_snapshot_json, hypotheses, user_verdict)`. Detects "idk why" / "something's off" / "no reason." Two-week-delayed job assembles surrounding context (calendar, sleep, who they texted, what they shipped), proposes 2+ candidate explanations (never 1 — that's the saccharine failure). User adjudicates; system tracks its own hit rate. EMA literature.

- **Weekly "ask shape" mirror** — `S, 1d`. New column on episodes (or `ask_shape` table) classifying each user turn. Weekly count in the consolidation. Framed as descriptive of the medium ("you were sharper this week. probably the deadline.") not prescriptive of character.

- **Decision log + Brier-score calibration** — _in top-5 roadmap_. Listed here for cross-reference.

---

## Notes for whoever picks the next batch

- The top 5 in the roadmap were ranked for **first-pass dependency** (caching unlocks cheap iteration; edge invalidation unlocks the cleaner facts layer for decision log; etc.). For a second batch, the dependency layer is mostly already paid — re-rank by "what does this user actually want."
- Several items here are clusters that could share infrastructure (e.g. the Reflection tree, A-MEM evolution, Procedural memory all want a unified `reflections` table; pair them).
- The MCP picks bias toward "more inputs for the noticing system" (Readwise, Apple Health, Apple Shortcuts) — the more raw context Hikari has, the more her existing rules-of-the-house produce real signal instead of generic chat.
- Sources for every item are in the original 5-agent research transcripts — see this session's date in the conversation history.
