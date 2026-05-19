"""One-shot ingest of to_memory.md into Hikari's memory.

Loads the structured user-context dump as:
  - core_blocks (always-injected) for identity / work / top-of-mind / personal / devices / instructions
  - facts table for granular retrievable items (anime, vocab, research, etc.) — embedded via bge-small
  - episodes table for thematic historical chunks — embedded

Idempotent on core_blocks (upsert). Facts/episodes use content checks before insert
so re-running won't duplicate.

Usage:
    uv run python scripts/ingest_to_memory.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Allow running from any cwd
sys.path.insert(0, str(Path(__file__).parent.parent))

from storage import db  # noqa: E402
from tools import embeddings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("ingest")


CORE_BLOCKS: dict[str, str] = {
    "user_profile": """name: Ol (Oleksandr Altukhov)
age: 29
nationality: Ukrainian
location: Kristiansund, Norway
partner: yes (no plans for children)""",

    "work_context": """current role: helsefagarbeider at Kringsjå langtid (Norwegian healthcare institution)
shift scheduling system: Visma Flyt Ressursstyring
education: medical degree, never practiced
prior career: marketing analytics
trajectory: actively building AI engineering / freelance skills
business vehicle: planned ENK (sole proprietorship) — not yet registered
business banking research: Lunar Business is the chosen fit (fixed monthly, Apple Pay, no transaction fees, Fiken integration)""",

    "top_of_mind": """current main project: Meria — persona "Mia" — AI Team OS for MLM (NOT a chatbot)
flow: 30-day onboarding → auto-graduate to leader → days 31-90 maintenance
architecture: multi-tenant, instance-per-team
first client: PM International (Y.O.U.R. Community, UA)
stack: Python 3.12 + uv, python-telegram-bot v21, Postgres 16 + pgvector, OpenRouter (Grok 4.1 Fast primary, Gemini 2.5 Flash Lite classifier), bge-m3 1024-dim embeddings, React + Vite Mini-App, Split-Brain Router
repo: github.com/Alksalt/meria

sister products in pipeline:
- interview-ai (Telegram intake + knowledge ingestion)
- sales-offers-bot (promo screenshot → vision → Postgres/Sheets → NL Q&A)

Claude Code usage concerns: on Max plan, considering Max 20x ($200/mo); frustrated by rapid token consumption from /compact and multi-agent sessions; tracking Opus 5 / Claude 5 (codename Fennec for Sonnet 5) expected Q2-Q3 2026.""",

    "personal_context": """hobbies: gaming (singleplayer + follows industry news), anime (400+ titles, AoT all-time favorite, Bleach origin story), gym + yoga + hiking, travel, food, tech/AI news
dislikes: AI slop
reading: psychology
content plans: TikTok / Reels for self-promotion
integrations: Google Calendar + Gmail connected with permission to write emails on his behalf""",

    "devices": """- Windows PC with RTX 5070 Ti
- PS5
- MacBook M3 Pro 18GB
- iPhone 17 Pro Max
- iPad (base)
- Mac Mini M4 16GB — dedicated to AI agent experiments. Currently exploring native Claude Code Agent Teams + Claude Agent SDK. OpenClaw and agent-forge confirmed abandoned.
- AirPods""",

    "instructions": """**Eng mode** — when Ol writes "Eng mode", respond per word with this exact 5-line structure (all English):
1) Word — short definition.
2) Synonyms: …
3) Memory: simple mnemonic.
4) Examples: 2 sentences.
5) Tip: short grammar or context tip.

**Email** — Claude has explicit permission to write emails on Ol's behalf via the Google / Gmail integration.""",
}


# (subject, predicate, object, importance, confidence)
FACTS: list[tuple[str, str, str, int, float]] = [
    # identity
    ("user", "full_name", "Oleksandr Altukhov", 9, 1.0),
    ("user", "preferred_name", "Ol", 9, 1.0),
    ("user", "age", "29", 8, 1.0),
    ("user", "nationality", "Ukrainian", 8, 1.0),
    ("user", "lives_in", "Kristiansund, Norway", 8, 1.0),
    ("user", "partner_status", "in a partnership, no plans for children", 7, 0.95),

    # work
    ("user", "works_at", "Kringsjå langtid healthcare institution", 8, 0.95),
    ("user", "role", "helsefagarbeider", 8, 0.95),
    ("user", "shift_system", "Visma Flyt Ressursstyring", 6, 0.9),
    ("user", "education", "medical degree, never practiced", 6, 0.9),
    ("user", "prior_career", "marketing analytics", 5, 0.9),

    # current project
    ("user", "main_project", "Meria — AI Team OS for MLM (persona 'Mia'). First client: PM International (Y.O.U.R. Community, UA).", 9, 0.95),
    ("user", "meria_repo", "github.com/Alksalt/meria", 6, 0.95),
    ("user", "meria_stack", "Python 3.12 + uv, ptb v21, Postgres 16 + pgvector, OpenRouter (Grok 4.1 Fast + Gemini 2.5 Flash Lite classifier), bge-m3 1024-dim, React+Vite Mini-App, Split-Brain Router", 7, 0.95),
    ("user", "pipeline_product", "interview-ai — Telegram intake + knowledge ingestion", 5, 0.9),
    ("user", "pipeline_product", "sales-offers-bot — promo screenshot → vision → Postgres/Sheets → NL Q&A", 5, 0.9),

    # business
    ("user", "business_vehicle_planned", "Norwegian ENK (sole proprietorship), not yet registered", 7, 0.9),
    ("user", "business_bank_choice", "Lunar Business (fixed monthly, Apple Pay, no transaction fees, Fiken integration)", 5, 0.9),
    ("user", "researched", "UAE tax residency and Dubai freelance/remote visa as a potential future path", 4, 0.8),
    ("user", "researched", "Norwegian tenancy law (husleieloven, rent reduction for construction disruption) for Kristiansund apartment", 4, 0.85),
    ("user", "tax_research", "ENK trygdeavgift + trinnskatt, MVA mechanics, Fiken for bookkeeping", 5, 0.9),

    # claude
    ("user", "anthropic_plan", "Max plan, considering Max 20x at $200/month", 5, 0.9),
    ("user", "frustration", "rapid token consumption from /compact and multi-agent sessions", 4, 0.85),
    ("user", "tracking_release", "Opus 5 / Claude 5 (codename Fennec for Sonnet 5) — Q2-Q3 2026", 4, 0.9),

    # anime
    ("user", "anime_all_time_favorite", "Attack on Titan", 8, 0.95),
    ("user", "anime_origin_story", "Bleach — first anime, still a deep favorite", 7, 0.9),
    ("user", "anime_count", "400+ titles watched", 4, 0.9),
    ("user", "finished_anime", "JJK Season 3", 4, 0.9),
    ("user", "watched_anime", "Neon Genesis Evangelion — critical but appreciative; found the final episodes disconnected", 5, 0.85),
    ("user", "currently_watching", "Witch Hat Atelier", 5, 0.9),
    ("user", "anime_anticipated", "Ghost in the Shell — Science SARU, July 2026, Prime Video", 4, 0.85),
    ("user", "anime_status_unknown", "Overlord Season 5", 3, 0.7),

    # personal
    ("user", "dislikes", "AI slop", 4, 0.9),
    ("user", "reading_interest", "psychology", 4, 0.85),
    ("user", "content_plan", "TikTok / Reels for self-promotion", 4, 0.85),

    # technical baseline
    ("user", "technical_baseline", "advanced Python + ML theory (transformers, classic ML, sklearn, PyTorch); now relies on AI assistance for building rather than coding from scratch", 6, 0.9),

    # devices (selected)
    ("user", "gpu", "RTX 5070 Ti on Windows PC", 4, 0.9),
    ("user", "phone", "iPhone 17 Pro Max", 3, 0.9),
    ("user", "ai_experiment_machine", "Mac Mini M4 16GB", 5, 0.9),

    # english learning
    ("user", "english_learning_active", "vocab list including: undermine, suck up to, silver lining, accommodation, account for, ubiquitous, give the benefit of the doubt, hinder, at the discretion of, hover, coulda/woulda/shoulda, deprived, gentrification, vibrant", 4, 0.85),
    ("user", "english_mode_format", "see 'instructions' core_block — 5-line per-word breakdown when he writes 'Eng mode'", 5, 0.95),

    # integrations
    ("user", "google_integration", "Google Calendar + Gmail connected; explicit permission to write emails on his behalf", 6, 0.95),
]


# (date, summary, importance)
EPISODES: list[tuple[str, str, int]] = [
    ("2026-04-19",
     "Returned from a vacation trip to Turkey (Istanbul, April 6-19, SAS Airlines). Many sessions covered real-time Istanbul travel logistics: transit (Istanbulkart, T1 tram, Havaist bus), food (kumpir, manti, dürüm kebab, Turkish breakfast), shopping (Kadıköy, Akasya AVM, Osmanbey), cruise ticketing, group itinerary planning for a multigenerational group. Drafted a Norwegian-language waste disposal complaint on return.",
     5),
    ("2026-04-25",
     "Deep buildout of Meria architecture. Multi-tenant stack locked: Python 3.12+uv, ptb v21, PG16+pgvector, OpenRouter (Grok 4.1 Fast primary, Gemini 2.5 Flash Lite classifier), bge-m3 1024-dim embeddings, React+Vite Mini-App, Split-Brain Router. Decided on instance-per-team isolation. First client: PM International (Y.O.U.R. Community, UA). Pricing: agency-comparable value, tiered monthly + setup fee, Norwegian MVA considerations for B2B exports.",
     8),
    ("2026-05-05",
     "Claude Code infrastructure deep-dives: parallel review-fix loops (Ralph loop vs /loop), multi-agent orchestration patterns (fan-out reviewers → plan synthesis → fan-out fixers), effort levels and model routing (Opus xhigh for planning, Sonnet for execution). Confirmed bug: plan mode incorrectly knocks users out of bypass mode. Compared GPT-5.5 Pro (strong on Terminal-Bench 2.0) vs Opus 4.7 (strong on SWE-Bench Pro). A/B tested DeepSeek V4-Flash and Grok 4.1 Fast across Meria's classifier. Built a hostile-no-praise GPT-5.5 Pro code review prompt with the GitHub connector.",
     7),
    ("2026-04-10",
     "Designed Turnus — AI-powered shift scheduling tool for Norwegian healthcare — through a deep discovery session. Outputs: TURNUS_PRODUCT.md, THINGS_TO_CONSIDER.md, a kickoff guide. Key decisions: LLM + Python validator loop, fully anonymized employee IDs, Norwegian output, consultant / decision-support positioning. First test case: his own workplace with his boss as initial client.",
     7),
    ("2026-03-15",
     "Earlier Meria/MLM bot foundations: A/B tested models (DeepSeek V3.2, Grok 4.1 Fast, Gemini 2.5 Flash Lite as classifier). Researched TTS options — Fish Audio S2 Pro recommended. Explored sales-offers-bot pipeline (Telegram screenshot → Vision API → Sheets → pgvector RAG → NL Q&A). Set up PostgreSQL via Docker, Google Sheets service account (resolved org policy bloat via fresh Gmail account), Fly.io deployment.",
     6),
    ("2025-12-01",
     "Multi-agent Claude Code workflows from scratch. OpenClaw (then primary, now abandoned). mlm-onboarding-bot foundations (Docker/PostgreSQL, pgvector, MiniLM-384 embeddings, OpenRouter integration). Registered @vidbir_bot — Ukrainian-language interviewer with RAG/pgvector. Built AI reference guide series (RAG, Prompt/Context Engineering, Agentic Architecture) as styled HTML. Built tg-time-logger gamification system.",
     5),
    ("2025-09-01",
     "Foundational technical baseline: advanced Python + ML theory (transformers, classic ML, sklearn, PyTorch). Career arc: healthcare worker → AI engineer/freelancer with ENK as the intended business vehicle. Built Notion 'From 0 to Hero' gamification productivity system before pivoting to client-facing products.",
     4),
]


def _existing_fact_keys() -> set[tuple[str, str, str]]:
    return {(f["subject"], f["predicate"], f["object"]) for f in db.active_facts(limit=10_000)}


def _existing_episode_dates() -> set[str]:
    return {e["date"] for e in db.recent_episodes(limit=10_000)}


async def _ingest_facts() -> int:
    existing = _existing_fact_keys()
    n = 0
    for subj, pred, obj, imp, conf in FACTS:
        if (subj, pred, obj) in existing:
            continue
        fid = db.insert_fact(subj, pred, obj, importance=imp, confidence=conf)
        try:
            emb = await embeddings.aembed(f"{subj} {pred} {obj}")
            db.set_vec_fact(fid, emb)
        except Exception:  # noqa: BLE001
            logger.exception("fact embed failed id=%s", fid)
        existing.add((subj, pred, obj))
        n += 1
    return n


async def _ingest_episodes() -> int:
    existing = _existing_episode_dates()
    n = 0
    for date_str, summary, imp in EPISODES:
        if date_str in existing:
            continue
        ep_id = db.insert_episode(date_str, summary, importance=imp)
        try:
            emb = await embeddings.aembed(summary)
            db.set_vec_episode(ep_id, emb)
        except Exception:  # noqa: BLE001
            logger.exception("episode embed failed id=%s", ep_id)
        existing.add(date_str)
        n += 1
    return n


async def main() -> int:
    for label, content in CORE_BLOCKS.items():
        db.upsert_core_block(label, content)
        logger.info("core_block %r: %d chars", label, len(content))

    facts_n = await _ingest_facts()
    episodes_n = await _ingest_episodes()
    logger.info("done: %d core_blocks, %d facts, %d episodes",
                len(CORE_BLOCKS), facts_n, episodes_n)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
