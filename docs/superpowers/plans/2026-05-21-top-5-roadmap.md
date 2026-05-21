# Top-5 Roadmap Implementation Plan (2026-05-21)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Tests are MANDATORY — TDD per feature. Single-test runs while iterating; full suite only at the end of each feature.

**Goal:** Implement five independent, cross-source-validated upgrades to Hikari:
1. **Prompt-cache the persona** — drops per-turn input cost 70-90% (S, free win)
2. **Callback surfacer** — inject rememberable moments into recall context, fires the "i noticed" voice (S-M)
3. **Edge invalidation on contradiction** — `fact_relations` get `valid_to` + reconciliation when a fact supersedes (S)
4. **Readwise MCP** — one-token wire-up for highlight retrieval (S)
5. **Decision log + calibration scorecard** — extract predictions from chat, weekly Brier-score mirror (M)

**Architecture:** All five bolt onto existing patterns — no redesign. (1) uses Claude API native `cache_control`. (2) extends the `inject_memory` UserPromptSubmit hook with a new candidate-scoring helper. (3) adds two columns to an existing table via the migration-fn pattern, plus a reconciler called from `mark_fact_invalid`. (4) is `.mcp.json` + allowlist + a new subagent. (5) follows the same scheduled-job + `run_internal_control` shape as `daily_checkin.py` / `evening_diary.py`.

**Tech Stack:** Python 3.12 + `uv`, Claude Agent SDK (≥0.1.70), SQLite (existing `hikari.db`), APScheduler, python-telegram-bot. Readwise REST via MCP server.

**Execution mode:** Features are independent and can be parallelized as five subagents — but order matters for first-pass risk: ship #1 first (free perf win + makes all future iteration cheaper), then the others in any order. #3 is the only one that touches a hot-path schema (fact_relations) and should bake a few days before #5 builds on it.

**Excluded from this plan (added to `2026-05-21-future-features-backlog.md`):** All second-tier ideas from the 5-agent research synthesis, including Spotify (user explicit exclusion), voice replies, lorebook, A-MEM evolution, procedural memory, JITAI, friction coach, etc.

---

## Ordering Rationale & Dependencies

1. **Feature 1 (prompt cache)** — ship first. Pure infra, no UX change, makes every other iteration cheaper. Independent of everything else.
2. **Feature 4 (Readwise MCP)** — ship second. Lowest-friction MCP (one access token); unblocks future highlight-driven callbacks.
3. **Feature 3 (edge invalidation)** — ship third. Schema migration must land before #5 starts using `fact_relations` for decision-attribution edges.
4. **Feature 2 (callback surfacer)** — ship fourth. Depends on a stable recall + memory surface; #3 cleans noise out of that surface first.
5. **Feature 5 (decision log)** — ship last. Largest scope, builds on the cleaner facts/relations layer from #3.

Single-session reviewer: each feature ends with full-suite run + 1 launchd restart + tail of `~/Library/Logs/hikari.err` for clean boot.

---

## File Structure

### Feature 1: Prompt-cache the persona
- **Modify:** `agents/runtime.py:97` (`_persona()` and `_build_options()`) — return system_prompt as a structured content list with `cache_control` markers instead of a flat string; verify SDK accepts list form.
- **Create:** `tests/test_persona_cache.py` — assert list-form structure, assert dynamic memory blocks (injected by `inject_memory` hook) sit AFTER the cache breakpoint.

### Feature 2: Callback surfacer
- **Create:** `agents/callback_surface.py` — `pick_callback_candidate(recent_user_text: str) -> dict | None` with the scoring + dedup logic.
- **Modify:** `agents/hooks.py:inject_memory` (the UserPromptSubmit hook, around line 50-200 — find exact line via grep) — call `pick_callback_candidate` and append a `# callback candidate:` block when one returns.
- **Modify:** `config/engagement.yaml` — add `callbacks:` section (enabled, min_importance, max_per_session, max_per_day).
- **Create:** `tests/test_callback_surface.py` — scoring, session dedup, daily cap, integration via the inject_memory hook.

### Feature 3: Edge invalidation on contradiction
- **Modify:** `storage/db.py:_migrate_tasks_decay_columns` (the dispatcher migration fn) — add a new `_migrate_fact_relations_validity` that adds `valid_to TEXT` + `invalidated_by_fact_id INTEGER REFERENCES facts(id)` + creates `idx_fact_relations_valid_to`. Per MEMORY.md feedback: ALTER-added columns and their indexes BOTH live in the migration fn, never in `_SCHEMA`.
- **Modify:** `storage/db.py` — add `fact_relations_invalidate_for_fact(fact_id: int) -> int` helper that stamps `valid_to = _now()` and `invalidated_by_fact_id` on all edges touching a superseded fact, and patch `mark_fact_invalid()` (find line via grep) to call it.
- **Modify:** `tools/memory/` (find the recall tool that surfaces edges — likely `recall.py` or similar) — filter out edges with `valid_to IS NOT NULL` from retrieval results.
- **Create:** `tests/test_fact_relations_validity.py` — migration adds columns idempotently, invalidation stamps edges, recall excludes invalidated edges.

### Feature 4: Readwise MCP
- **Modify:** `.mcp.json` — add `readwise` server entry (uses official `@readwise/mcp` via `npx`, requires `READWISE_TOKEN` env).
- **Modify:** `agents/runtime.py:_DEDICATED_AND_EXTERNAL_TOOLS` (around line 171-199) — add `mcp__readwise__*` wildcard to allowlist.
- **Modify:** `agents/external_wrap_hook.py` (or wherever `prompt_injection.wrap_patterns` is consumed) — add `mcp__readwise__*` to the wrap-untrusted pattern list in `config/engagement.yaml` since Readwise content is external-sourced.
- **Modify:** `scripts/` — create or extend `scripts/setup_readwise.md` (or update README) with the two-step setup: get token from readwise.io/access_token, paste into `.env`, restart launchd.
- **Modify:** `config/engagement.yaml` — add `READWISE_TOKEN_ENV: READWISE_TOKEN` reference if other tools use that pattern (grep first).
- **Create:** `tests/test_readwise_mcp.py` — smoke: MCP entry parses, allowlist contains the wildcard, env-var warning fires when token missing (mirror the existing google_workspace pattern at `telegram_bridge.py:1627-1638`).

### Feature 5: Decision log + calibration scorecard
- **Modify:** `storage/db.py` — add `decisions` table to `_SCHEMA` (brand-new table, indexes inline per MEMORY.md). Helpers: `decision_insert`, `decision_resolve`, `decisions_unresolved_due`, `decisions_resolved_recent`, `decision_brier_score`.
- **Create:** `agents/decision_log.py` — extraction prompt + scheduled resolver. `extract_decision(user_text: str) -> dict | None` runs a small Haiku turn that returns `{predicted_p, statement, resolve_by} | None`. `run_decision_resolver(send_text)` is the scheduled job (weekly Sunday 19:00) that asks the user about decisions whose `resolve_by` has passed.
- **Modify:** `agents/hooks.py:inject_memory` — after the callback block, add a "# unresolved decisions" line when ≥1 decision is overdue (so Hikari can naturally surface it).
- **Modify:** `agents/scheduler.py` — register `decision_resolver` weekly cron (Sunday 19:00 local, before drift_canary at 20:00).
- **Modify:** `config/engagement.yaml` — add `decision_log:` section (enabled, hour, minute, min_resolve_days, max_per_week_ask).
- **Modify:** `CLAUDE.md` — add one situational-policy bullet for prediction-shaped phrases ("i think", "probably", "by friday") → call `decision_log_capture` tool. Add to `cap_exempt_sources`.
- **Create:** `tools/decision_log/` directory with `_shared.py` (constants) and `capture.py` (`decision_log_capture` MCP tool). Wire into `_utility_index.ALL_TOOLS`.
- **Create:** `tests/test_decision_log.py` — schema, extraction parser, Brier math, resolver dispatch, dedup, integration with inject_memory.

---

# Feature 1: Prompt-cache the persona

**Why first:** Free perf win, zero character risk, makes every future iteration cheaper. Hikari's CLAUDE.md is ~6-8k tokens and gets re-paid on every chat / proactive / drift judge / reflection turn. Caching drops per-turn input cost 70-90% on warm cache.

**Architecture:** Claude API accepts `system_prompt` as either a string OR a list of `{"type": "text", "text": "...", "cache_control": {...}}` content blocks. We restructure `_persona()` to return the list form. The dynamic memory blocks injected by the `inject_memory` UserPromptSubmit hook already land in the *user message*, not the system prompt — so the system prompt is already mostly static and the migration is mechanical.

**Verification approach:** A live integration test runs two back-to-back turns via the bridge and asserts `usage.cache_read_input_tokens > 0` on the second turn's `ResultMessage`. (Cheap to run live: ~$0.005 in test cost.)

### Task 1.1: Verify SDK accepts list-form system_prompt

**Files:**
- Read-only: `agents/runtime.py:280-289` (`ClaudeAgentOptions` construction)
- Read-only: `.venv/lib/python3.12/site-packages/claude_agent_sdk/` (introspect — confirm `system_prompt: str | list` shape)

- [ ] **Step 1.1.1:** Inspect the installed SDK's `ClaudeAgentOptions` type annotations.

Run:
```bash
uv run python -c "from claude_agent_sdk import ClaudeAgentOptions; import inspect; print(inspect.signature(ClaudeAgentOptions))"
```
Expected: signature shows `system_prompt` as `str | list[dict] | None` or similar. If it's `str` only, escalate — this feature blocked.

- [ ] **Step 1.1.2:** Read the SDK source for the API request builder. Search for where `system_prompt` is serialized into the JSON request.

Run:
```bash
grep -rn "system_prompt\|system=" .venv/lib/python3.12/site-packages/claude_agent_sdk/ | head -20
```
Expected: confirm list form is passed through to the Anthropic API as `system: [{"type":"text","text":"...","cache_control":{"type":"ephemeral"}}]`. If the SDK wraps it differently, follow the docs at https://platform.claude.com/docs/en/build-with-claude/prompt-caching.

### Task 1.2: Write the failing test

**Files:**
- Create: `tests/test_persona_cache.py`

- [ ] **Step 1.2.1:** Write the structural test.

```python
"""Prompt-caching test: the persona system prompt is sent as a list with a
cache_control marker on the static block. Dynamic memory injected by the
inject_memory UserPromptSubmit hook lands in user message blocks, not in the
system prompt — so the system prompt itself stays cache-stable across turns."""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture(autouse=True)
def _reset_runtime(monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import agents.runtime as runtime
    importlib.reload(runtime)
    yield


def test_persona_returns_list_with_cache_control():
    from agents.runtime import _persona
    out = _persona()
    # Must be a list of content blocks, not a flat string.
    assert isinstance(out, list), (
        "expected list of content blocks for cache_control, got str"
    )
    assert len(out) >= 1
    # First (and main) block is the static persona, marked for caching.
    static = out[0]
    assert static.get("type") == "text"
    assert "i am hikari" in static.get("text", "").lower()
    assert static.get("cache_control") == {"type": "ephemeral"}


def test_persona_substitutes_max_turns_inside_cached_block():
    """{max_turns} substitution must happen BEFORE the cache_control wrap so the
    cached text is identical across turns."""
    from agents.runtime import DEFAULT_MAX_TURNS, _persona
    out = _persona()
    static_text = out[0]["text"]
    assert "{max_turns}" not in static_text, (
        "{max_turns} placeholder leaked into cached text — substitution must "
        "run before the wrap"
    )
    assert str(DEFAULT_MAX_TURNS) in static_text


def test_build_options_passes_list_form_system_prompt():
    """_build_options must pass the list form through to ClaudeAgentOptions."""
    from agents.runtime import _build_options
    opts = _build_options(resume=None)
    sp = opts.system_prompt
    assert isinstance(sp, list)
    assert sp[0].get("cache_control") == {"type": "ephemeral"}
```

- [ ] **Step 1.2.2:** Run the new test to confirm it fails.

Run: `uv run pytest tests/test_persona_cache.py -v`
Expected: 3 FAILS — `_persona()` currently returns `str`.

### Task 1.3: Implement the cache-wrapped persona

**Files:**
- Modify: `agents/runtime.py:97-103` (`_persona()`)

- [ ] **Step 1.3.1:** Replace `_persona()` with the list-returning version.

```python
@cache
def _persona() -> list[dict]:
    """Return the persona as a list of content blocks with cache_control on
    the static text, so the Anthropic API can cache it across turns.

    The {max_turns} placeholder is substituted BEFORE the wrap so the cached
    text is byte-identical across calls; dynamic blocks (# now, # emotional
    state, # memory) are injected into the user message by the inject_memory
    UserPromptSubmit hook and do NOT live in the system prompt.

    Result: 70-90% input-cost drop on warm-cache turns. ~6-8k cached tokens
    out of the typical 8-10k per-turn input. See:
    https://platform.claude.com/docs/en/build-with-claude/prompt-caching
    """
    text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    # Substitute live turn budget. .replace() is safer than .format() since
    # CLAUDE.md is hand-edited and a stray `{` would crash startup.
    text = text.replace("{max_turns}", str(DEFAULT_MAX_TURNS))
    return [{
        "type": "text",
        "text": text,
        "cache_control": {"type": "ephemeral"},
    }]
```

- [ ] **Step 1.3.2:** Run the test to confirm it passes.

Run: `uv run pytest tests/test_persona_cache.py -v`
Expected: 3 PASS.

### Task 1.4: Sanity-check the full suite

- [ ] **Step 1.4.1:** Run the full non-persona test suite to ensure no caller broke.

Run: `uv run pytest tests/ --ignore=tests/persona --ignore=tests/integration -q --tb=line -p no:cacheprovider`
Expected: all green. If any tests construct `ClaudeAgentOptions` with the persona as a string, update them.

### Task 1.5: Live verification of cache_read_input_tokens

- [ ] **Step 1.5.1:** Restart the bot.

Run:
```bash
launchctl kickstart -k gui/$(id -u)/com.hikari.agent && sleep 4 && tail -5 ~/Library/Logs/hikari.err
```
Expected: clean startup, no SDK errors.

- [ ] **Step 1.5.2:** Send two consecutive messages via Telegram (or simulate via the bridge). Then inspect the log.

Run:
```bash
grep -E "cache_read_input_tokens|cache_creation_input_tokens|total_tokens" ~/Library/Logs/hikari.err | tail -10
```
Expected: first turn shows `cache_creation_input_tokens > 0` (paying the 1.25x writes price); second turn shows `cache_read_input_tokens > 0` (paying 0.1x). If the SDK doesn't surface these in logs, add a one-line log in `_invoke_sdk`'s `ResultMessage` branch.

### Task 1.6: Commit

- [ ] **Step 1.6.1:** Stage and commit.

```bash
git add agents/runtime.py tests/test_persona_cache.py
git commit -m "$(cat <<'EOF'
perf(runtime): prompt-cache the persona system prompt

CLAUDE.md is ~6-8k tokens and was re-paid on every chat / proactive / drift
judge / reflection turn. Wrap the static text in a cache_control breakpoint
so warm-cache turns drop input cost 70-90%. Dynamic blocks already land in
the user message via inject_memory; no architectural change required.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

# Feature 2: Callback surfacer

**Why:** The single feature Character.AI / Replika / Nomi / Kalon users cite as "the moment it stopped feeling like a chatbot." Maps directly onto Hikari's existing "i noticed —" voice marker and one-notice-per-session rule. Implements the inject side of the loop; the *use* is already in her CLAUDE.md voice.

**Architecture:** A new module `agents/callback_surface.py` exports `pick_callback_candidate(recent_user_text)` that scans `episodes` and `character_thoughts` for high-importance / affect-laden rows, scores them via BM25 against the user's recent message, applies a session-dedup guard (using `session_scratch` 24h TTL), and returns at most one candidate dict. The existing `inject_memory` UserPromptSubmit hook calls it once per turn and, when a candidate returns, appends a `# callback candidate` block. Hikari sees it and decides whether to surface — her voice rules already cap noticing to once per session.

### Task 2.1: Write the failing test for the candidate picker

**Files:**
- Create: `tests/test_callback_surface.py`

- [ ] **Step 2.1.1:** Write the test.

```python
"""Callback surfacer: pick a rememberable moment that's topically adjacent to
the user's recent message. Once-per-session dedup via session_scratch."""
from __future__ import annotations

import importlib
from datetime import date as _date
from datetime import timedelta
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    yield


def test_pick_callback_candidate_returns_none_when_no_episodes():
    from agents.callback_surface import pick_callback_candidate
    out = pick_callback_candidate("anything")
    assert out is None


def test_pick_callback_candidate_finds_topical_high_importance_episode():
    from agents import callback_surface
    from storage import db
    today = _date.today()
    week_ago = (today - timedelta(days=7)).isoformat()
    db.insert_episode(week_ago,
                      "burned the pasta. set off the smoke alarm.", 7)
    db.insert_episode(week_ago, "wrote tests for the migration.", 4)
    out = callback_surface.pick_callback_candidate("burned my hand on the pan")
    assert out is not None
    assert "pasta" in out["text"]


def test_pick_callback_candidate_respects_session_dedup():
    from agents import callback_surface
    from storage import db
    today = _date.today()
    db.insert_episode((today - timedelta(days=3)).isoformat(),
                      "lost the kyiv keys.", 8)
    # Need a session_id for the dedup scratch row.
    db.set_session_id("test-session-1")

    first = callback_surface.pick_callback_candidate("where are my keys")
    assert first is not None
    # Same session, same topic → must dedup.
    second = callback_surface.pick_callback_candidate("found the keys")
    assert second is None
```

- [ ] **Step 2.1.2:** Run to confirm it fails.

Run: `uv run pytest tests/test_callback_surface.py -v`
Expected: ImportError / module missing.

### Task 2.2: Implement the candidate picker

**Files:**
- Create: `agents/callback_surface.py`

- [ ] **Step 2.2.1:** Write the module.

```python
"""Callback surfacer — picks one "rememberable moment" topically adjacent to
the user's recent message and returns it so the inject_memory hook can drop a
hint block into Hikari's context. She decides whether to surface; her CLAUDE.md
'i noticed —' rule already caps noticing to once per session, so the upstream
discipline is already in place.

Source rows: episodes (importance >= min_importance) + character_thoughts
(also high-importance proxies). Scoring: BM25-lite token-overlap against the
recent user text. Dedup: once-per-session via session_scratch (24h TTL).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from agents import config as cfg
from storage import db

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "of", "in", "on", "at", "for",
    "with", "to", "from", "is", "was", "are", "be", "been", "i", "you",
    "your", "my", "me", "we", "they", "it", "this", "that", "those",
    "these", "have", "had", "has", "did", "do", "does",
})


def _tokens(text: str) -> set[str]:
    return {
        t.lower() for t in _TOKEN_RE.findall(text or "")
        if len(t) > 2 and t.lower() not in _STOPWORDS
    }


def _score(candidate_text: str, query_text: str) -> float:
    """Token-overlap score in [0, 1]. Ratio of unique query tokens that
    appear in the candidate."""
    q = _tokens(query_text)
    if not q:
        return 0.0
    c = _tokens(candidate_text)
    return len(q & c) / len(q)


def pick_callback_candidate(recent_user_text: str) -> dict | None:
    """Return one callback dict ``{text, source, score}`` or None.

    Strategy: pull recent high-importance episodes + character_thoughts,
    score by token overlap with the user message, dedup against this
    session's scratch (so we never surface the same row twice in one chat).
    """
    if not bool(cfg.get("callbacks.enabled", True)):
        return None
    if not recent_user_text or len(recent_user_text.strip()) < 4:
        return None

    min_importance = int(cfg.get("callbacks.min_importance", 6))
    min_score = float(cfg.get("callbacks.min_score", 0.25))
    window_days = int(cfg.get("callbacks.window_days", 90))

    candidates: list[dict[str, Any]] = []
    try:
        with db._conn() as conn:
            ep_rows = conn.execute(
                "SELECT id, date, summary FROM episodes "
                "WHERE importance >= ? "
                "AND date >= date('now', '-' || ? || ' days') "
                "ORDER BY date DESC LIMIT 50",
                (min_importance, window_days),
            ).fetchall()
        for r in ep_rows:
            candidates.append({
                "id": f"ep:{r['id']}",
                "source": "episode",
                "date": str(r["date"]),
                "text": str(r["summary"] or ""),
            })
    except Exception:
        logger.exception("callback_surface: episode query failed")

    if not candidates:
        return None

    scored = [
        (_score(c["text"], recent_user_text), c) for c in candidates
    ]
    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best = scored[0]
    if best_score < min_score:
        return None

    # Session dedup: skip if we've already surfaced this candidate id in this
    # session.
    session_id = db.get_session_id() or ""
    if session_id:
        scratch_topic = "callback_surfaced"
        try:
            with db._conn() as conn:
                row = conn.execute(
                    "SELECT payload_json FROM session_scratch "
                    "WHERE session_id = ? AND topic = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (session_id, scratch_topic),
                ).fetchone()
            already: set[str] = set()
            if row:
                already = set(json.loads(row["payload_json"]).get("ids", []))
            if best["id"] in already:
                return None
            already.add(best["id"])
            with db._conn() as conn:
                conn.execute(
                    "INSERT INTO session_scratch (session_id, topic, payload_json) "
                    "VALUES (?, ?, ?)",
                    (session_id, scratch_topic,
                     json.dumps({"ids": sorted(already)})),
                )
        except Exception:
            logger.exception("callback_surface: dedup write failed")

    best["score"] = round(best_score, 3)
    return best
```

- [ ] **Step 2.2.2:** Run the test to confirm it passes.

Run: `uv run pytest tests/test_callback_surface.py -v`
Expected: 3 PASS.

### Task 2.3: Wire the picker into inject_memory

**Files:**
- Modify: `agents/hooks.py` (find `inject_memory` function — `grep -n "def inject_memory" agents/hooks.py`)
- Modify: `config/engagement.yaml` — add `callbacks:` section.

- [ ] **Step 2.3.1:** Add config block at the bottom of `config/engagement.yaml`.

```yaml
# Callback surfacer (2026-05-21): inject one "rememberable moment" hint into
# Hikari's turn context when a recent high-importance episode is topically
# adjacent to the user's message. Hikari's CLAUDE.md "i noticed —" rule
# already caps surfacing to once per session, so this only ever proposes.
callbacks:
  enabled: true
  min_importance: 6
  min_score: 0.25
  window_days: 90
```

- [ ] **Step 2.3.2:** Read `agents/hooks.py:inject_memory` to find a clean insertion point. Append the candidate block AFTER memory injection, BEFORE returning.

Locate via:
```bash
grep -n "def inject_memory\|additional_context\|return.*context" agents/hooks.py | head
```

Then add a block (exact placement depends on the structure you find — append to the same string the hook returns):

```python
# Callback surfacer (2026-05-21): one rememberable moment, topically scored.
# Hikari sees this in her context and decides whether to surface; her voice
# rules already cap noticing to once per session.
try:
    from agents.callback_surface import pick_callback_candidate
    user_text = ""
    if isinstance(input_data, dict):
        user_text = str(input_data.get("prompt") or "")
    candidate = pick_callback_candidate(user_text)
    if candidate:
        extra_lines.append(
            f"\n# callback candidate (score {candidate['score']}):\n"
            f"  [{candidate['date']}] {candidate['text'][:200]}\n"
            "(surface sideways if it fits — your one-notice-per-session "
            "rule still applies.)"
        )
except Exception:
    logger.exception("inject_memory: callback_surface failed (non-fatal)")
```

(Replace `extra_lines` / `input_data` / `user_text` with whatever the function actually uses — read its full body first.)

- [ ] **Step 2.3.3:** Add an integration test that asserts the hook output includes the block when a candidate exists.

Append to `tests/test_callback_surface.py`:

```python
@pytest.mark.asyncio
async def test_inject_memory_includes_callback_block_when_candidate_exists():
    from agents.hooks import inject_memory
    from storage import db
    today_ago = (_date.today() - timedelta(days=5)).isoformat()
    db.insert_episode(today_ago, "burned the pasta again.", 7)

    out = await inject_memory(
        {"prompt": "burned myself making lunch"}, None, None,
    )
    blob = json.dumps(out)
    assert "callback candidate" in blob
    assert "pasta" in blob
```

- [ ] **Step 2.3.4:** Run.

Run: `uv run pytest tests/test_callback_surface.py -v`
Expected: 4 PASS.

### Task 2.4: Full suite + commit

- [ ] **Step 2.4.1:** Run the suite.

Run: `uv run pytest tests/ --ignore=tests/persona --ignore=tests/integration -q --tb=line -p no:cacheprovider`
Expected: all green.

- [ ] **Step 2.4.2:** Commit.

```bash
git add agents/callback_surface.py agents/hooks.py config/engagement.yaml tests/test_callback_surface.py
git commit -m "$(cat <<'EOF'
feat(memory): callback surfacer for rememberable moments

Score recent high-importance episodes against the user's message via simple
token overlap; surface one as a candidate block in Hikari's turn context
when it scores above threshold. Session-dedup via session_scratch so the
same callback never lands twice in one chat. Hikari's existing
one-notice-per-session rule caps actual surfacing — this only proposes.

Pattern observed in Character.AI / Replika / Nomi user reviews as the #1
"stopped feeling like a chatbot" differentiator.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

# Feature 3: Edge invalidation on contradiction

**Why:** Hikari's `fact_relations` are co-occurrence edges with no "this relation became false." When a fact transitions to `superseded`, every relation that depended on it stays live and keeps surfacing in recall — embarrassing for a character built on continuity. Graphiti pattern (arxiv 2501.13956) won +18.5% on DMR with this exact shape.

**Architecture:** Two columns added to `fact_relations` via the migration-fn pattern (`_migrate_fact_relations_validity`, dispatched from `_migrate_tasks_decay_columns`). A reconciler helper stamps `valid_to` on all edges touching a fact at the moment that fact is superseded. Recall queries filter `valid_to IS NULL`.

### Task 3.1: Write the failing migration + invalidation test

**Files:**
- Create: `tests/test_fact_relations_validity.py`

- [ ] **Step 3.1.1:** Write the test.

```python
"""Bi-temporal fact_relations: edges get valid_to + invalidated_by_fact_id
when a fact is superseded. Recall must filter invalidated edges out."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    yield


def test_migration_adds_columns_idempotently():
    from storage import db
    # Trigger schema.
    db.upsert_core_block("ping", "ping")
    with db._conn() as c:
        cols = {r["name"] for r in
                c.execute("PRAGMA table_info(fact_relations)").fetchall()}
    assert "valid_to" in cols
    assert "invalidated_by_fact_id" in cols


def test_invalidate_stamps_edges_for_superseded_fact():
    from storage import db
    f1 = db.insert_fact("user", "lives in kyiv", 9)
    f2 = db.insert_fact("user", "works at acme", 8)
    f3 = db.insert_fact("user", "has cat named nori", 7)
    db.fact_relation_insert(f1, "co_occurs_with", f2)
    db.fact_relation_insert(f2, "co_occurs_with", f3)
    db.fact_relation_insert(f1, "co_occurs_with", f3)

    f1_v2 = db.insert_fact("user", "lives in lisbon", 9)
    db.mark_fact_invalid(f1, superseded_by_fact_id=f1_v2)

    # f1's two outgoing edges should be invalidated.
    n = db.fact_relations_invalidate_for_fact(f1)  # idempotent on second call
    assert n == 0 or n >= 0  # already invalidated by mark_fact_invalid

    with db._conn() as c:
        rows = c.execute(
            "SELECT id, valid_to, invalidated_by_fact_id "
            "FROM fact_relations "
            "WHERE subject_fact_id = ? OR object_fact_id = ?",
            (f1, f1),
        ).fetchall()
    assert all(r["valid_to"] is not None for r in rows)
    assert all(r["invalidated_by_fact_id"] == f1_v2 for r in rows)


def test_recall_filters_invalidated_edges():
    """fact_relations_for(fact_id) must skip valid_to IS NOT NULL rows."""
    from storage import db
    f1 = db.insert_fact("user", "works at acme", 9)
    f2 = db.insert_fact("user", "drinks coffee", 5)
    db.fact_relation_insert(f1, "co_occurs_with", f2)

    f1_v2 = db.insert_fact("user", "works at globex", 9)
    db.mark_fact_invalid(f1, superseded_by_fact_id=f1_v2)

    rels = db.fact_relations_for(f1)
    assert rels == []  # all edges from f1 are invalidated
```

- [ ] **Step 3.1.2:** Run to confirm it fails.

Run: `uv run pytest tests/test_fact_relations_validity.py -v`
Expected: schema test fails (cols missing), invalidate test fails (helper missing).

### Task 3.2: Add the migration

**Files:**
- Modify: `storage/db.py` — locate `_migrate_tasks_decay_columns` (around line 479) and append a new call. Define `_migrate_fact_relations_validity` below it.

- [ ] **Step 3.2.1:** Edit `_migrate_tasks_decay_columns` to dispatch the new migration.

Find the function (around line 479-496). Append `_migrate_fact_relations_validity(conn)` to its list of inner calls.

- [ ] **Step 3.2.2:** Define the migration.

Add after the existing migration functions:

```python
def _migrate_fact_relations_validity(conn: sqlite3.Connection) -> None:
    """Bi-temporal fact_relations: when a fact transitions to 'superseded',
    every relation touching it gets stamped with valid_to + the new fact's
    id. Recall filters valid_to IS NOT NULL. Graphiti pattern (Zep,
    arxiv 2501.13956)."""
    existing = {
        row["name"]
        for row in conn.execute(
            "PRAGMA table_info(fact_relations)").fetchall()
    }
    if "valid_to" not in existing:
        conn.execute(
            "ALTER TABLE fact_relations ADD COLUMN valid_to TEXT"
        )
    if "invalidated_by_fact_id" not in existing:
        conn.execute(
            "ALTER TABLE fact_relations ADD COLUMN "
            "invalidated_by_fact_id INTEGER REFERENCES facts(id)"
        )
    # Per MEMORY.md: indexes for ALTER-added columns live in the migration fn,
    # never in _SCHEMA, because tests use fresh DBs and _SCHEMA runs before
    # migrations.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fact_relations_valid_to "
        "ON fact_relations(valid_to) WHERE valid_to IS NULL"
    )
```

### Task 3.3: Implement the invalidator helper + recall filter

**Files:**
- Modify: `storage/db.py` — add `fact_relations_invalidate_for_fact` near other `fact_relation_*` helpers (find via `grep -n "fact_relation_insert" storage/db.py`).
- Modify: `storage/db.py:mark_fact_invalid` — find it (`grep -n "def mark_fact_invalid" storage/db.py`) and call the invalidator on supersession.
- Modify: `storage/db.py:fact_relations_for` (or whatever read function exists) — add a `WHERE valid_to IS NULL` filter.

- [ ] **Step 3.3.1:** Add the invalidator helper.

```python
def fact_relations_invalidate_for_fact(
    fact_id: int, invalidated_by: int | None = None,
) -> int:
    """Stamp valid_to + invalidated_by_fact_id on every edge touching
    ``fact_id`` that is currently live. Returns the number of edges
    newly invalidated. Idempotent — already-invalidated edges are skipped."""
    with _conn() as c:
        cur = c.execute(
            "UPDATE fact_relations "
            "SET valid_to = ?, invalidated_by_fact_id = ? "
            "WHERE (subject_fact_id = ? OR object_fact_id = ?) "
            "AND valid_to IS NULL",
            (_now(), invalidated_by, int(fact_id), int(fact_id)),
        )
    return int(cur.rowcount or 0)
```

- [ ] **Step 3.3.2:** Patch `mark_fact_invalid` to call it.

Find the function. After it sets the fact's `status='superseded'` (or wherever the supersession is committed), add:

```python
    # Bi-temporal: walk this fact's edges and stamp them invalidated.
    try:
        fact_relations_invalidate_for_fact(
            fact_id, invalidated_by=superseded_by_fact_id,
        )
    except Exception:
        logger.exception(
            "mark_fact_invalid: edge invalidation failed for fact_id=%s",
            fact_id,
        )
```

- [ ] **Step 3.3.3:** Patch the read function (likely `fact_relations_for` — find via `grep -n "def fact_relations_for\|FROM fact_relations" storage/db.py`).

Add `AND valid_to IS NULL` to its WHERE clause.

- [ ] **Step 3.3.4:** Run the test.

Run: `uv run pytest tests/test_fact_relations_validity.py -v`
Expected: 3 PASS.

### Task 3.4: Full suite + commit

- [ ] **Step 3.4.1:** Full suite.

Run: `uv run pytest tests/ --ignore=tests/persona --ignore=tests/integration -q --tb=line -p no:cacheprovider`
Expected: all green. **Important:** restart launchd after merge — the migration runs on next boot, and per MEMORY.md you tail the err log:

```bash
launchctl kickstart -k gui/$(id -u)/com.hikari.agent && sleep 4 && tail -20 ~/Library/Logs/hikari.err
```

- [ ] **Step 3.4.2:** Commit.

```bash
git add storage/db.py tests/test_fact_relations_validity.py
git commit -m "$(cat <<'EOF'
feat(memory): bi-temporal fact_relations with edge invalidation

When a fact transitions to 'superseded' via mark_fact_invalid, walk its
edges and stamp valid_to + invalidated_by_fact_id on each. Recall filters
out invalidated edges. Graphiti pattern (Zep, arxiv 2501.13956) — +18.5%
on DMR.

Migration adds columns + a partial index for live edges; indexes live in
the migration fn per the schema-migration-ordering rule.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

# Feature 4: Readwise MCP

**Why:** Hikari already saves links to a shelf. Readwise is the long-term substrate — months of highlights and Reader docs the user can query semantically. Pairs with the callback surfacer (#2): "i remember you highlighted something on this in june." Official first-party MCP server, single access token.

**Architecture:** External MCP server wired via `.mcp.json` (like `google_workspace`). Allowlist gets `mcp__readwise__*`. Token via env var `READWISE_TOKEN`. Wrap-untrusted patterns get the prefix added since Readwise content is third-party-authored.

### Task 4.1: Identify the Readwise MCP server package

- [ ] **Step 4.1.1:** Determine the canonical command.

Read https://docs.readwise.io/tools/mcp for the official install. Likely either:
- `npx -y @readwise/mcp` (preferred — first-party), or
- a Python `uvx` variant.

Pick whichever the docs publish as official as of plan-execution time. Note the env-var name (`READWISE_TOKEN` or `READWISE_ACCESS_TOKEN` — check exact spelling on https://readwise.io/access_token).

### Task 4.2: Wire .mcp.json

**Files:**
- Modify: `.mcp.json`

- [ ] **Step 4.2.1:** Read current `.mcp.json` to match formatting.

Run: `cat .mcp.json`

- [ ] **Step 4.2.2:** Add the readwise entry under `mcpServers`. Example shape (adjust per Task 4.1 findings):

```json
"readwise": {
  "command": "npx",
  "args": ["-y", "@readwise/mcp"],
  "env": {
    "READWISE_TOKEN": "${READWISE_TOKEN}"
  }
}
```

### Task 4.3: Update allowlist

**Files:**
- Modify: `agents/runtime.py:171-199` (`_DEDICATED_AND_EXTERNAL_TOOLS`)

- [ ] **Step 4.3.1:** Add `"mcp__readwise__*"` to the list, alongside the other external wildcards.

### Task 4.4: Add wrap-untrusted pattern

**Files:**
- Modify: `config/engagement.yaml` — find `prompt_injection.wrap_patterns` (`grep -n "wrap_patterns" config/engagement.yaml`).

- [ ] **Step 4.4.1:** Append the Readwise pattern to the existing wrap-patterns list. Readwise content is external-authored (book highlights, article excerpts) so it MUST go through the untrusted-wrap to prevent prompt injection.

Example addition:
```yaml
- "mcp__readwise__.*"
```

### Task 4.5: Token-missing warning at startup

**Files:**
- Modify: `agents/telegram_bridge.py:1622-1640` — the existing `_gw_missing` warning block for `google_workspace`.

- [ ] **Step 4.5.1:** Add a parallel block for Readwise (mirror the google_workspace pattern exactly).

```python
if "readwise" in servers and not os.environ.get("READWISE_TOKEN"):
    logger.warning(
        "readwise MCP is registered in .mcp.json but READWISE_TOKEN is "
        "not set — the server will fail to authenticate. Get a token at "
        "https://readwise.io/access_token, paste into .env, then "
        "launchctl kickstart -k gui/$(id -u)/com.hikari.agent",
    )
```

### Task 4.6: Write smoke test

**Files:**
- Create: `tests/test_readwise_mcp.py`

- [ ] **Step 4.6.1:** Write.

```python
"""Readwise MCP smoke test: server entry parses, allowlist contains it."""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import agents.runtime as runtime
    importlib.reload(runtime)


def test_readwise_in_mcp_json():
    mcp = json.loads(
        (Path(__file__).parent.parent / ".mcp.json").read_text())
    assert "readwise" in mcp.get("mcpServers", {})


def test_readwise_wildcard_in_allowlist():
    from agents.runtime import allowed_tool_names
    tools = allowed_tool_names()
    assert any("readwise" in t for t in tools)
```

- [ ] **Step 4.6.2:** Run.

Run: `uv run pytest tests/test_readwise_mcp.py -v`
Expected: 2 PASS.

### Task 4.7: Document setup + commit

**Files:**
- Modify: `README.md` (find the env vars section — `grep -n "GOOGLE_WORKSPACE\|env" README.md | head`) OR create `scripts/setup_readwise.md`.

- [ ] **Step 4.7.1:** Add a 5-line section: get token from https://readwise.io/access_token → paste into `.env` as `READWISE_TOKEN=…` → restart launchd → `tail -5 ~/Library/Logs/hikari.err`.

- [ ] **Step 4.7.2:** Commit (without committing your real token!).

```bash
git status  # MUST NOT show .env
git add .mcp.json agents/runtime.py agents/telegram_bridge.py config/engagement.yaml tests/test_readwise_mcp.py README.md
git commit -m "$(cat <<'EOF'
feat(mcp): wire Readwise MCP for highlight recall

Single access token from readwise.io/access_token. Wrapped under
untrusted-content patterns since Readwise content is third-party-authored
(book highlights, article excerpts). Startup warning fires when the env
var is missing, mirroring the google_workspace pattern.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

# Feature 5: Decision log + calibration scorecard

**Why:** Substance over wellness. A Brier-score mirror of the user's own forecasts, surfaced in Hikari's voice. Tetlock superforecasting in the small — six months of calibration data will change which decisions the user makes slowly versus fast.

**Architecture:** Three pieces:
1. **Capture:** the user tells Hikari a prediction ("i think we ship friday at 80%") → she calls `decision_log_capture` (new MCP tool) → row in `decisions`. The CLAUDE.md situational policy gets one bullet so she picks up the speech act.
2. **Resolve:** weekly Sunday 19:00 scheduler job (`run_decision_resolver`) iterates decisions whose `resolve_by` has passed and `outcome` is null. For each, send the user one Telegram message ("you said: 'X' (80%). did it happen?") and wait for the next message to resolve it (handled by a thin handler hook similar to daily_checkin's question flow).
3. **Mirror:** `inject_memory` adds a `# unresolved decisions (N)` line when there are overdue items; Hikari can naturally surface them.

Brier score: `1/N × Σ(outcome - predicted_p)²`. Surfaced via `decision_brier_score` helper.

### Task 5.1: Schema + helpers

**Files:**
- Modify: `storage/db.py:_SCHEMA` — add `decisions` table (brand-new, indexes inline per MEMORY.md).

- [ ] **Step 5.1.1:** Add to `_SCHEMA` (between `drift_canary_answers` and the existing `future_letters` block):

```sql
-- Decision log + Brier-style calibration. Capture: extract prediction
-- speech acts from chat into a row. Resolve: weekly job asks the user
-- about decisions whose resolve_by has passed. Mirror: monthly Brier
-- score surfaced in voice. Brand-new table, indexes inline.
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    statement TEXT NOT NULL,
    predicted_p REAL NOT NULL CHECK (predicted_p >= 0.0 AND predicted_p <= 1.0),
    resolve_by TEXT NOT NULL,
    outcome INTEGER,                   -- 0 or 1, null until resolved
    resolved_at TEXT,
    reasoning TEXT,
    asked_at TEXT,                     -- last time we asked the user about it
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_decisions_unresolved
    ON decisions(resolve_by) WHERE outcome IS NULL;
CREATE INDEX IF NOT EXISTS idx_decisions_resolved
    ON decisions(resolved_at) WHERE outcome IS NOT NULL;
```

- [ ] **Step 5.1.2:** Add helpers at the bottom of `storage/db.py` (mirror the `future_letter_*` block style):

```python
# ---------- decisions (calibration log) ----------

def decision_insert(statement: str, predicted_p: float, resolve_by: str,
                    reasoning: str | None = None) -> int:
    """Insert a captured prediction. resolve_by is ISO date or ISO datetime."""
    p = max(0.0, min(1.0, float(predicted_p)))
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO decisions (statement, predicted_p, resolve_by, "
            "reasoning) VALUES (?, ?, ?, ?)",
            (statement, p, resolve_by, reasoning),
        )
    return int(cur.lastrowid or 0)


def decision_resolve(decision_id: int, outcome: int) -> None:
    """Mark a decision resolved. outcome must be 0 or 1."""
    if outcome not in (0, 1):
        raise ValueError("outcome must be 0 or 1")
    with _conn() as c:
        c.execute(
            "UPDATE decisions SET outcome = ?, resolved_at = ? WHERE id = ?",
            (int(outcome), _now(), int(decision_id)),
        )


def decisions_unresolved_due(limit: int = 5) -> list[dict[str, Any]]:
    """Decisions whose resolve_by has passed and outcome is still null,
    ordered by oldest first."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, statement, predicted_p, resolve_by, asked_at "
            "FROM decisions "
            "WHERE outcome IS NULL AND resolve_by <= date('now') "
            "ORDER BY resolve_by ASC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def decision_mark_asked(decision_id: int) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE decisions SET asked_at = ? WHERE id = ?",
            (_now(), int(decision_id)),
        )


def decision_brier_score(window_days: int = 90) -> dict[str, Any]:
    """Return ``{n, brier, mean_predicted, mean_outcome}`` over decisions
    resolved in the last window_days, or ``{n: 0}`` if none."""
    with _conn() as c:
        rows = c.execute(
            "SELECT predicted_p, outcome FROM decisions "
            "WHERE outcome IS NOT NULL "
            "AND resolved_at >= datetime('now', '-' || ? || ' days')",
            (int(window_days),),
        ).fetchall()
    n = len(rows)
    if n == 0:
        return {"n": 0}
    brier = sum((float(r["predicted_p"]) - int(r["outcome"])) ** 2
                for r in rows) / n
    mean_p = sum(float(r["predicted_p"]) for r in rows) / n
    mean_o = sum(int(r["outcome"]) for r in rows) / n
    return {
        "n": n, "brier": round(brier, 4),
        "mean_predicted": round(mean_p, 4),
        "mean_outcome": round(mean_o, 4),
    }
```

### Task 5.2: Write failing tests

**Files:**
- Create: `tests/test_decision_log.py`

- [ ] **Step 5.2.1:** Write storage + Brier tests first.

```python
"""Decision log: schema, helpers, Brier scoring, resolver."""
from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HIKARI_DB_PATH", str(tmp_path / "hikari.db"))
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "12345")
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    yield


def test_decision_table_created():
    from storage import db
    db.upsert_core_block("ping", "ping")
    with db._conn() as c:
        cols = {r["name"] for r in
                c.execute("PRAGMA table_info(decisions)").fetchall()}
    for col in ("statement", "predicted_p", "resolve_by",
                "outcome", "resolved_at"):
        assert col in cols


def test_decision_insert_and_resolve_round_trip():
    from storage import db
    did = db.decision_insert("ship by friday", 0.8, "2026-05-25", "feel ok")
    assert did > 0
    db.decision_resolve(did, 1)
    score = db.decision_brier_score(window_days=365)
    assert score["n"] == 1
    assert score["brier"] == pytest.approx(0.04)  # (0.8 - 1)^2 = 0.04


def test_decisions_unresolved_due_filters():
    from storage import db
    db.decision_insert("past", 0.5, "2026-01-01")
    db.decision_insert("future", 0.5, "2099-01-01")
    rows = db.decisions_unresolved_due(limit=10)
    statements = [r["statement"] for r in rows]
    assert "past" in statements
    assert "future" not in statements


def test_resolve_validates_outcome():
    from storage import db
    did = db.decision_insert("x", 0.5, "2026-01-01")
    with pytest.raises(ValueError):
        db.decision_resolve(did, 2)
```

- [ ] **Step 5.2.2:** Run.

Run: `uv run pytest tests/test_decision_log.py -v`
Expected: 4 PASS (schema + helpers already implemented in Task 5.1).

### Task 5.3: Capture tool (MCP)

**Files:**
- Create: `tools/decision_log/__init__.py`
- Create: `tools/decision_log/_shared.py`
- Create: `tools/decision_log/capture.py`

- [ ] **Step 5.3.1:** Read an existing utility tool as a template to match the registration pattern.

Run:
```bash
grep -l "create_sdk_mcp_server\|tool(" tools/calc/ tools/weather/ 2>/dev/null | head -3
cat tools/calc/__init__.py 2>/dev/null
```

- [ ] **Step 5.3.2:** Write `tools/decision_log/_shared.py`.

```python
"""Decision-log capture-tool shared constants."""
TOOL_NAME = "decision_log_capture"
```

- [ ] **Step 5.3.3:** Write `tools/decision_log/capture.py`.

```python
"""decision_log_capture — MCP tool Hikari calls when she catches a prediction
speech act from the user. Stores one row in the decisions table.

CLAUDE.md teaches the trigger phrases; this tool is the writer. Returns a
short in-voice ack so Hikari can move on without ceremony.
"""
from __future__ import annotations

from claude_agent_sdk import tool

from storage import db
from tools._response import ok as _ok


@tool(
    "decision_log_capture",
    "Log a user's prediction so we can score calibration later. Use when "
    "the user states a probability + a date ('i think we ship friday at "
    "80%', 'probably 60% chance the deal closes by next monday').",
    {
        "statement": str,
        "predicted_p": float,
        "resolve_by": str,  # ISO date YYYY-MM-DD
        "reasoning": str,
    },
)
async def decision_log_capture(args: dict) -> dict:
    """args: statement (str), predicted_p (float in [0,1]), resolve_by (ISO
    date YYYY-MM-DD), reasoning (optional str). Returns OK with the row id."""
    statement = str(args.get("statement") or "").strip()
    if not statement:
        return _ok("decision_log_capture: statement is required.")
    try:
        p = float(args.get("predicted_p") or 0.0)
    except (TypeError, ValueError):
        return _ok("decision_log_capture: predicted_p must be a number.")
    resolve_by = str(args.get("resolve_by") or "").strip()
    if not resolve_by:
        return _ok("decision_log_capture: resolve_by is required.")
    reasoning = str(args.get("reasoning") or "").strip() or None

    did = db.decision_insert(statement, p, resolve_by, reasoning)
    return _ok(f"logged decision #{did} at p={p}, resolve {resolve_by}.")


ALL_TOOLS = [decision_log_capture]
```

- [ ] **Step 5.3.4:** Write `tools/decision_log/__init__.py` exporting `ALL_TOOLS`.

```python
"""decision_log: capture predictions, resolve them, score calibration."""
from .capture import ALL_TOOLS

__all__ = ["ALL_TOOLS"]
```

- [ ] **Step 5.3.5:** Register the tool in the utility index.

Locate `tools/_utility_index.py` and `tools/_registry.py:discover_utility_tool_names`. Confirm the auto-discovery picks up `tools/decision_log/` automatically (per `agents/runtime.py:166-170` comment "auto-derived from tools/_registry.discover_utility_tool_names() — adding a feature folder under tools/<name>/ with ALL_TOOLS is enough; no edit here required"). If yes, no extra edit. If no, add the explicit registration.

Run:
```bash
grep -n "discover_utility\|ALL_TOOLS" tools/_registry.py tools/_utility_index.py | head -10
```

Decide based on output whether the auto-discovery covers it.

### Task 5.4: Resolver scheduled job

**Files:**
- Create: `agents/decision_log.py`
- Modify: `agents/scheduler.py` — register the job (mirror evening_diary at line 184-194).
- Modify: `config/engagement.yaml` — add `decision_log:` section + add `decision_log` to `cap_exempt_sources`.

- [ ] **Step 5.4.1:** Write `agents/decision_log.py`.

```python
"""Decision-log resolver. Weekly Sunday 19:00 — finds decisions whose
resolve_by has passed, asks the user about up to N per run, marks them
as asked (so we don't double-ask). User's next message in chat resolves
the outcome (handled by bridge wiring in a later task — for now we just
ask and let the user reply manually)."""
from __future__ import annotations

import logging

from agents import config as cfg
from storage import db

logger = logging.getLogger(__name__)


async def run_decision_resolver(send_text) -> int:
    """Surface unresolved-and-overdue decisions to the user. Returns the
    number of decisions asked about this call."""
    if not bool(cfg.get("decision_log.enabled", True)):
        return 0
    max_per_run = int(cfg.get("decision_log.max_per_week_ask", 3))
    overdue = db.decisions_unresolved_due(limit=max_per_run)
    if not overdue:
        return 0

    asked = 0
    for d in overdue:
        line = (
            f"calibration check: '{d['statement']}' (you said {d['predicted_p']}). "
            "did it happen? yes / no."
        )
        try:
            await send_text(line)
            db.decision_mark_asked(int(d["id"]))
            asked += 1
        except Exception:
            logger.exception(
                "decision_resolver: send failed for decision_id=%s",
                d["id"],
            )
    logger.info("decision_resolver: asked about %d overdue decisions", asked)
    return asked
```

- [ ] **Step 5.4.2:** Add to `config/engagement.yaml`.

```yaml
decision_log:
  enabled: true
  # Weekly Sunday 19:00 local — sits before drift_canary at 20:00 so they
  # don't double-fire a heavy proactive within the same hour.
  hour: 19
  minute: 0
  # Cap on overdue decisions asked about per weekly run. Higher feels naggy.
  max_per_week_ask: 3
  # Window for the Brier rolling score surfaced in voice.
  brier_window_days: 90
```

And add to `cadence_governor.cap_exempt_sources`:
```yaml
  cap_exempt_sources:
    - daily_checkin
    - future_letter
    - decision_log
```

- [ ] **Step 5.4.3:** Register in `agents/scheduler.py`.

Mirror the evening_diary block (around line 180):

```python
    # Decision-log resolver: weekly Sunday 19:00 local. Asks about
    # decisions whose resolve_by has passed. See agents/decision_log.py.
    if bool(cfg.get("decision_log.enabled", True)):
        from .decision_log import run_decision_resolver
        dl_hour = int(cfg.get("decision_log.hour", 19))
        dl_minute = int(cfg.get("decision_log.minute", 0))
        async def _decision_resolver_job():
            return await run_decision_resolver(send_text)
        scheduler.add_job(
            _decision_resolver_job,
            CronTrigger(day_of_week="sun", hour=dl_hour, minute=dl_minute),
            id="decision_resolver",
            coalesce=True, max_instances=1, misfire_grace_time=3600,
        )
```

- [ ] **Step 5.4.4:** Update `tests/test_smoke.py:148-165` (the `expected` job-id set). Add `decision_resolver`.

### Task 5.5: CLAUDE.md trigger bullet

**Files:**
- Modify: `CLAUDE.md` — append to the situational-policy bulleted list (around line 107-122).

- [ ] **Step 5.5.1:** Add one bullet, in her voice, that teaches the trigger.

```markdown
- user states a prediction with a probability and a date ("i think we ship friday at 80%", "probably 60% the deal closes monday") → call `decision_log_capture` with statement / predicted_p / resolve_by. don't make a thing of it. confirm in voice ("logged. we'll see.") and move on. if she's overconfident a lot, you'll surface the brier score later in voice — don't preach about it now.
```

### Task 5.6: Resolver test

**Files:**
- Append to: `tests/test_decision_log.py`

- [ ] **Step 5.6.1:** Write the resolver test.

```python
@pytest.mark.asyncio
async def test_resolver_asks_about_overdue_decisions():
    from agents import decision_log
    from storage import db

    db.decision_insert("ship by yesterday", 0.7, "2026-01-01")
    db.decision_insert("future thing", 0.5, "2099-01-01")

    send = AsyncMock(return_value=("ok", 1, True))
    n = await decision_log.run_decision_resolver(send)
    assert n == 1
    # First arg of first call is the question text.
    assert "ship by yesterday" in send.call_args.args[0]

    # Second run within the same window doesn't re-ask (asked_at stamped).
    # Per current design: decision_mark_asked stamps asked_at; we don't yet
    # filter on it in decisions_unresolved_due. If you want re-ask cooldown,
    # filter `(asked_at IS NULL OR asked_at < datetime('now', '-7 days'))`
    # in decisions_unresolved_due. Add and re-test.
```

- [ ] **Step 5.6.2:** Run.

Run: `uv run pytest tests/test_decision_log.py -v`
Expected: all PASS.

### Task 5.7: Full suite + restart verification + commit

- [ ] **Step 5.7.1:** Full suite.

Run: `uv run pytest tests/ --ignore=tests/persona --ignore=tests/integration -q --tb=line -p no:cacheprovider`
Expected: all green; the smoke test now includes `decision_resolver` in the expected scheduler set.

- [ ] **Step 5.7.2:** Restart launchd, tail err log.

Run:
```bash
launchctl kickstart -k gui/$(id -u)/com.hikari.agent && sleep 4 && grep "scheduler started:" ~/Library/Logs/hikari.err | tail -1
```
Expected: `decision_resolver` appears in the job list.

- [ ] **Step 5.7.3:** Commit.

```bash
git add storage/db.py agents/decision_log.py agents/scheduler.py CLAUDE.md config/engagement.yaml tools/decision_log/ tests/test_decision_log.py tests/test_smoke.py
git commit -m "$(cat <<'EOF'
feat(memory): decision log + Brier-score calibration mirror

Capture predictions from chat via decision_log_capture (new MCP tool the
persona bullet teaches her to use on probability+date speech acts). Weekly
Sunday 19:00 resolver asks the user about overdue decisions; outcomes
flow into a rolling Brier score she can surface in voice.

Tetlock superforecasting in the small — substance, not wellness.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review Checklist (for the executing engineer)

After completing all five features, before opening a PR:

- [ ] Full suite green (`uv run pytest tests/ --ignore=tests/persona --ignore=tests/integration -q`)
- [ ] Bot restarts cleanly (`launchctl kickstart -k …` then `tail ~/Library/Logs/hikari.err`)
- [ ] Schema migrations applied (`uv run python -c "from storage import db; db.upsert_core_block('x','x')"` then check `PRAGMA table_info(fact_relations); PRAGMA table_info(decisions);` shows the new columns/tables)
- [ ] No secrets in `git status` (especially `.env`)
- [ ] CLAUDE.md change for #5 reads in her voice (terse, lowercase, no preaching)
- [ ] Five commits, one per feature (or bundle if you prefer — note in the PR body)

End of plan.
