# Four-feature batch (cheap & compound) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Tests are MANDATORY — TDD per feature. Single-test runs while iterating; full suite at the end of each feature.

**Goal:** Ship four cheap, compound features that activate plumbing Hikari already has but doesn't use: gap-awareness (turns "you went quiet" voice line into actual signal), actor-aware attribution on facts (lets contradiction resolution favor user-stated facts over inferred), Apple Shortcuts MCP (one bridge → every Shortcut becomes a tool), and YouTube Transcript MCP (drop a link, get a summary).

**Architecture:** All four bolt onto existing patterns — zero redesign.
- **Feature A (attribution):** ALTER `facts` table with `attribution TEXT` column via the migration-fn pattern; thread it through `insert_fact` → `remember` → `reflection.py` writers. Recall scoring stays unchanged (NULL = neutral); future scorer changes are out of scope for this batch.
- **Feature B (gap awareness):** Read `runtime_state.last_user_message` in `agents/hooks.py:inject_memory`, compute delta from `# now`, inject a `# gap_since_last:` line when ≥2h. Three bands map to the voice grammar Hikari already has in CLAUDE.md.
- **Feature C/D (MCPs):** Two new entries in `.mcp.json`, two new wildcards in `agents/runtime.py:_DEDICATED_AND_EXTERNAL_TOOLS`. YouTube Transcript also gets a `prompt_injection.wrap_patterns` entry (external/untrusted content).

**Tech Stack:** Python 3.12 + `uv`, Claude Agent SDK (≥0.1.70), SQLite (existing `hikari.db`), pytest, npx-installed MCP servers (`mcp-server-apple-shortcuts`, `mcp-youtube-transcript`).

**Execution mode:** Four features in order: A → B → C → D. Each is one commit. Order rationale: A first (schema migration; safest pure-additive), B next (touches hot inject_memory path; smaller blast radius once schema is settled), C+D last (MCP-only; trivially reversible by removing the .mcp.json entry).

---

## File Structure

### Feature A: Actor-aware attribution
- **Modify:** `storage/db.py` — add `_migrate_facts_attribution` migration fn (adds `attribution TEXT` column to `facts`); register it in the migration chain after `_migrate_facts_recall_decay`. Thread `attribution` kwarg through `insert_fact` (line 809-838). New `db.fact_set_attribution(fact_id, attribution)` helper for retrofitting.
- **Modify:** `tools/memory/remember.py` — accept optional `attribution` arg (defaults to `user_stated` since `remember` is invoked by Hikari in response to user statements). Pass through to `insert_fact`.
- **Modify:** `agents/reflection.py` — three `insert_fact` call sites (lines 127, 144, 638): set `attribution="hikari_inferred"` (reflection extracts facts via her own pass).
- **Create:** `tests/test_facts_attribution.py` — schema migration is idempotent, insert_fact accepts attribution, remember defaults to user_stated, reflection sets hikari_inferred, legacy NULL rows untouched.

### Feature B: Streak/gap awareness
- **Modify:** `agents/hooks.py` — add `_format_gap_since_last()` helper near other `_format_*` helpers (around line 250-330). Call it from `inject_memory` after `_format_now`. Three bands: <2h returns empty string (invisible), 2h-24h returns soft `# gap_since_last: Xh` line, >24h returns `# gap_since_last: Xd (long quiet — your "you went quiet. that's disruptive" line applies)`.
- **Modify:** `config/engagement.yaml` — add `gap_awareness:` block with `enabled: true`, `soft_threshold_hours: 2`, `long_threshold_hours: 24`.
- **Create:** `tests/test_gap_awareness.py` — formatter returns "" under threshold, soft band format, long band format, integration through inject_memory mock.

### Feature C: Apple Shortcuts MCP
- **Modify:** `.mcp.json` — add `apple_shortcuts` server entry (npx, no env).
- **Modify:** `agents/runtime.py` — add `mcp__apple_shortcuts__*` to `_DEDICATED_AND_EXTERNAL_TOOLS` (line 200-205).
- **Modify:** `README.md` — add Apple Shortcuts to the macOS native integrations section (TCC ritual note, same as apple_events).
- **Create:** `tests/test_apple_shortcuts_mcp.py` — `.mcp.json` parses, allowlist contains the wildcard, no wrap_pattern needed (local trusted).

### Feature D: YouTube Transcript MCP
- **Modify:** `.mcp.json` — add `youtube_transcript` server entry (npx, no env, no auth).
- **Modify:** `agents/runtime.py` — add `mcp__youtube_transcript__*` to `_DEDICATED_AND_EXTERNAL_TOOLS`.
- **Modify:** `config/engagement.yaml` — add `^mcp__youtube_transcript__` to `prompt_injection.wrap_patterns` (line 727-755). Transcript content is external/untrusted (videos can say anything).
- **Create:** `tests/test_youtube_transcript_mcp.py` — `.mcp.json` parses, allowlist contains the wildcard, wrap_patterns contains the regex.

---

## Ordering & Dependencies

1. **Feature A (attribution)** — ship first. Pure-additive schema; doesn't change any recall behavior today (NULL = neutral). Threading writers through means future features can rely on it.
2. **Feature B (gap awareness)** — ship second. Hot path (inject_memory) but small and well-bounded. Schema (`runtime_state.last_user_message`) is already populated by 5 call sites. Visible in voice immediately.
3. **Feature C (Apple Shortcuts MCP)** — ship third. Trivial: 3 lines in .mcp.json + 1 line in allowlist + README note. Restart launchd to load.
4. **Feature D (YouTube Transcript MCP)** — ship last. Same shape as C but adds a wrap_patterns entry. Mirrors the existing notion/google_workspace external-wrap pattern.

After each feature: full pytest + launchd restart + tail `~/Library/Logs/hikari.err` for clean boot.

---

# Feature A: Actor-aware attribution

**Why:** The `facts.source` column today is free-text and mostly NULL. Adding a structured `attribution` enum makes contradiction resolution favor what the user actually said over what subagents inferred — the Mem0 2026 pattern. Pure-additive; no recall behavior change today.

**Architecture:** New `attribution TEXT` column on `facts`. Five allowed values (documented but not enforced at DB level — keeping it TEXT for flexibility): `user_stated` (Hikari heard you say it), `user_observed` (inferred from your actions), `hikari_inferred` (reflection extracted from chat), `subagent_extracted` (an explorer/research agent surfaced it), `external_source` (came from a tool result like email/wiki). NULL = legacy/unknown. Recall scoring stays unchanged in this batch; the column is captured so future scorer work has data to use.

### Task A.1: Write the failing schema test

**Files:**
- Create: `tests/test_facts_attribution.py`

- [ ] **Step A.1.1:** Write the schema migration test.

```python
"""facts.attribution column — pure-additive enum for tagging where a fact
came from. NULL = legacy/unknown. Tested values: user_stated, user_observed,
hikari_inferred, subagent_extracted, external_source.

The column lands via a migration fn (not _SCHEMA) because tests use fresh DBs
and the project's MEMORY.md schema-migration-ordering rule requires
ALTER-added columns to be applied in migrations, not the schema bootstrap."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from storage import db


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    """Fresh per-test DB. Mirrors tests/test_facts_recall_decay.py:23-39."""
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    import storage.db as _db_mod
    importlib.reload(_db_mod)
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db._reset_schema_sentinel()
    yield


def test_facts_has_attribution_column():
    """Fresh DB after migration chain has facts.attribution."""
    # Trigger schema/migration application via a no-op _conn() open.
    with db._conn() as c:
        cols = {row["name"] for row in c.execute("PRAGMA table_info(facts)").fetchall()}
    assert "attribution" in cols


def test_facts_attribution_migration_idempotent():
    """Running migrations twice doesn't blow up (e.g. duplicate ALTER)."""
    with db._conn() as c:
        cols = [row["name"] for row in c.execute("PRAGMA table_info(facts)").fetchall()]
    assert cols.count("attribution") == 1
    # Force re-run of schema bootstrap path.
    db._reset_schema_sentinel()
    with db._conn() as c:
        cols2 = [row["name"] for row in c.execute("PRAGMA table_info(facts)").fetchall()]
    assert cols2.count("attribution") == 1
```

- [ ] **Step A.1.2:** Run the test to confirm it fails.

Run: `uv run pytest tests/test_facts_attribution.py::test_facts_has_attribution_column -xvs`
Expected: FAIL with `assert "attribution" in cols` (column doesn't exist yet).

### Task A.2: Implement the migration

**Files:**
- Modify: `storage/db.py` — add `_migrate_facts_attribution` after `_migrate_facts_recall_decay`; chain it from `_migrate_facts_recall_decay`.

- [ ] **Step A.2.1:** Add the migration function.

```python
def _migrate_facts_attribution(conn: sqlite3.Connection) -> None:
    """Actor-aware attribution column on facts.

    Documented values (not enforced at DB level):
      user_stated         — user told Hikari directly
      user_observed       — inferred from user's actions, not stated
      hikari_inferred     — Hikari's own reflection extracted from chat
      subagent_extracted  — an explorer/research subagent surfaced it
      external_source     — came from a tool result (email, wiki, MCP)

    NULL = legacy/unknown. Recall scoring currently treats NULL as neutral.
    Pure-additive: no behavior change at recall today.
    """
    existing = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(facts)").fetchall()
    }
    if "attribution" not in existing:
        conn.execute("ALTER TABLE facts ADD COLUMN attribution TEXT")
    # No index — attribution is read alongside the fact row, not queried by.
```

- [ ] **Step A.2.2:** Chain it into the migration sequence. Find the line in `_migrate_facts_recall_decay` (or whichever migration fn calls the next one) that delegates to the next migration; add `_migrate_facts_attribution(conn)` at the appropriate spot.

```python
# At the end of _migrate_facts_recall_decay (or wherever the chain
# currently terminates), add:
_migrate_facts_attribution(conn)
```

- [ ] **Step A.2.3:** Run the schema test.

Run: `uv run pytest tests/test_facts_attribution.py -xvs`
Expected: PASS on both schema tests.

### Task A.3: Thread attribution through insert_fact

**Files:**
- Modify: `storage/db.py:809-838` — add `attribution: str | None = None` kwarg to `insert_fact`; include in the INSERT.

- [ ] **Step A.3.1:** Add the failing test for insert_fact accepting attribution.

```python
def test_insert_fact_accepts_attribution():
    """insert_fact takes an optional attribution kwarg and stores it.
    Uses the autouse _isolated fixture above for a fresh DB."""
    fact_id = db.insert_fact(
        subject="user", predicate="likes", object_="cold rice",
        attribution="user_stated",
    )
    with db._conn() as c:
        row = c.execute(
            "SELECT attribution FROM facts WHERE id = ?", (fact_id,)
        ).fetchone()
    assert row["attribution"] == "user_stated"


def test_insert_fact_attribution_defaults_null():
    """Without attribution kwarg, the column is NULL (preserves legacy behavior)."""
    fact_id = db.insert_fact(
        subject="user", predicate="likes", object_="something",
    )
    with db._conn() as c:
        row = c.execute(
            "SELECT attribution FROM facts WHERE id = ?", (fact_id,)
        ).fetchone()
    assert row["attribution"] is None
```

- [ ] **Step A.3.2:** Run — confirm both fail (one with `TypeError: unexpected keyword`, the other with `AttributeError` from missing column).

Run: `uv run pytest tests/test_facts_attribution.py::test_insert_fact_accepts_attribution tests/test_facts_attribution.py::test_insert_fact_attribution_defaults_null -xvs`

- [ ] **Step A.3.3:** Modify `insert_fact` signature + body.

```python
def insert_fact(
    subject: str,
    predicate: str,
    object_: str,
    importance: int = 5,
    confidence: float = 0.9,
    source_message_id: int | None = None,
    source: str | None = None,
    attribution: str | None = None,
) -> int:
    """Insert a new fact. Returns row id. ...

    ``attribution`` is the structured provenance tag (one of:
    user_stated, user_observed, hikari_inferred, subagent_extracted,
    external_source) — see _migrate_facts_attribution for semantics."""
    now = _now()
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO facts (subject, predicate, object, confidence, importance, "
            "valid_from, source_message_id, source, attribution, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
            (subject, predicate, object_, confidence, importance, now,
             source_message_id, source, attribution, now),
        )
        fact_id = cur.lastrowid
        c.execute(
            "INSERT INTO fts (content, kind, ref_id) VALUES (?, 'fact', ?)",
            (f"{subject} {predicate} {object_}", fact_id),
        )
    return fact_id
```

- [ ] **Step A.3.4:** Run — confirm both pass.

Run: `uv run pytest tests/test_facts_attribution.py -xvs`

### Task A.4: Wire up `remember` tool

**Files:**
- Modify: `tools/memory/remember.py:42` — pass `attribution="user_stated"` to insert_fact. (The `remember` tool is invoked by Hikari in response to a user statement; by definition the fact comes from `user_stated`.)

- [ ] **Step A.4.1:** Add the failing test.

```python
def test_remember_tool_tags_user_stated():
    """The remember tool stores facts with attribution='user_stated'.
    Uses the autouse _isolated fixture above."""
    import asyncio
    from tools.memory.remember import remember
    result = asyncio.run(remember({
        "subject": "user", "predicate": "owns", "object": "macbook m3",
    }))
    fact_id = result["data"]["fact_id"]
    with db._conn() as c:
        row = c.execute(
            "SELECT attribution FROM facts WHERE id = ?", (fact_id,)
        ).fetchone()
    assert row["attribution"] == "user_stated"
```

- [ ] **Step A.4.2:** Run — confirm it fails (attribution is NULL).

- [ ] **Step A.4.3:** Modify `tools/memory/remember.py:42`.

```python
# Old:
#     new_id = db.insert_fact(subject, predicate, object_, importance, confidence)
# New:
new_id = db.insert_fact(
    subject, predicate, object_, importance, confidence,
    attribution="user_stated",
)
```

- [ ] **Step A.4.4:** Run — confirm pass.

Run: `uv run pytest tests/test_facts_attribution.py::test_remember_tool_tags_user_stated -xvs`

### Task A.5: Wire up `reflection.py` writers

**Files:**
- Modify: `agents/reflection.py:127, 144, 638` — three `insert_fact` call sites get `attribution="hikari_inferred"`.

- [ ] **Step A.5.1:** Add the failing test (skip if running reflection.py in test is fragile — instead read the source).

```python
def test_reflection_call_sites_pass_attribution():
    """Each db.insert_fact call in agents/reflection.py passes
    attribution='hikari_inferred' — reflection extracts facts via Hikari's
    own LLM pass, not from a direct user statement."""
    import re
    src = open("agents/reflection.py").read()
    # Find all insert_fact( ... ) call bodies up to closing paren depth 0.
    # Each must include attribution='hikari_inferred' (or "hikari_inferred").
    call_starts = [m.start() for m in re.finditer(r"db\.insert_fact\(", src)]
    assert len(call_starts) >= 3, f"expected ≥3 insert_fact calls, found {len(call_starts)}"
    for start in call_starts:
        # Walk paren depth.
        depth = 0
        i = start + len("db.insert_fact")
        while i < len(src):
            if src[i] == "(": depth += 1
            elif src[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        call_body = src[start:i + 1]
        assert "hikari_inferred" in call_body, (
            f"insert_fact call at offset {start} missing "
            f"attribution='hikari_inferred':\n{call_body}"
        )
```

- [ ] **Step A.5.2:** Run — confirm fail.

- [ ] **Step A.5.3:** Edit each of the three call sites. Example for line 127:

```python
# Before:
fact_id = db.insert_fact(
    subject=f["subject"],
    predicate=f["predicate"],
    object_=f["object"],
    confidence=f.get("confidence", 0.85),
    importance=f.get("importance", 5),
)
# After:
fact_id = db.insert_fact(
    subject=f["subject"],
    predicate=f["predicate"],
    object_=f["object"],
    confidence=f.get("confidence", 0.85),
    importance=f.get("importance", 5),
    attribution="hikari_inferred",
)
```

Apply the same change at lines 144 and 638. Use `grep -n "db.insert_fact" agents/reflection.py` to find the exact lines (they may shift after the first edit).

- [ ] **Step A.5.4:** Run — confirm pass.

Run: `uv run pytest tests/test_facts_attribution.py -xvs`

### Task A.6: Full suite + commit

- [ ] **Step A.6.1:** Run the full suite.

Run: `uv run pytest -q`
Expected: 889 + new tests passing (892 if 3 added), 0 failed.

- [ ] **Step A.6.2:** Commit.

```bash
git add storage/db.py tools/memory/remember.py agents/reflection.py tests/test_facts_attribution.py
git commit -m "feat(memory): actor-aware attribution column on facts

Adds facts.attribution TEXT column via _migrate_facts_attribution
migration fn. Threaded through insert_fact + remember tool
(user_stated) + reflection.py writers (hikari_inferred). NULL =
legacy/unknown. Recall scoring unchanged today; future contradiction
resolution can read this column to favor user_stated over inferred.

Five documented values: user_stated, user_observed, hikari_inferred,
subagent_extracted, external_source."
```

**Feature A success criteria:**
- ✅ `facts.attribution` column exists on fresh DB + existing DB (idempotent ALTER)
- ✅ `insert_fact(... attribution=X)` writes the value
- ✅ `remember()` tags `user_stated`
- ✅ all three reflection insert_fact sites tag `hikari_inferred`
- ✅ full pytest green
- ✅ launchd restart clean: `launchctl kickstart -k gui/$(id -u)/com.hikari.agent && sleep 3 && tail -20 ~/Library/Logs/hikari.err` — no schema errors

---

# Feature B: Streak/gap awareness

**Why:** Hikari's CLAUDE.md already has the line "you went quiet. that's disruptive" — but she has no current data on how long the user has actually been quiet. `runtime_state.last_user_message` is already populated by 5 call sites (runtime.py:486 + telegram_bridge.py:544/658/992/1033). Reading it in `inject_memory` and injecting a gap-since-last line turns the existing voice into actual signal.

**Architecture:** Three bands based on the existing re-engagement thresholds:
- **<2h** — invisible (no injection; she's mid-conversation)
- **2h–24h** — soft notice: `# gap_since_last: 4h` (lets her drop a callback if it fits)
- **>24h** — strong signal: `# gap_since_last: 2d (long quiet — your "you went quiet. that's disruptive" rule applies)` (the inline parenthetical instructs her to use the voice line)

Config-driven thresholds with sensible defaults. No new schema.

### Task B.1: Write the failing test

**Files:**
- Create: `tests/test_gap_awareness.py`

- [ ] **Step B.1.1:** Write the formatter tests.

```python
"""Gap awareness — inject a # gap_since_last: line into inject_memory
when the user has been quiet ≥2h. Three bands: <2h invisible,
2h-24h soft, >24h strong (triggers the existing 'you went quiet'
voice line).

The gap is computed from runtime_state.last_user_message vs now."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agents import hooks


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def test_gap_under_2h_returns_empty():
    """<2h elapsed → no gap line (she's mid-conversation)."""
    now = datetime(2026, 5, 21, 14, 0, tzinfo=UTC)
    last = now - timedelta(hours=1, minutes=30)
    out = hooks._format_gap_since_last(_iso(last), now=now)
    assert out == ""


def test_gap_soft_band_2h_to_24h():
    """2h-24h elapsed → soft '# gap_since_last: 4h' line."""
    now = datetime(2026, 5, 21, 14, 0, tzinfo=UTC)
    last = now - timedelta(hours=4)
    out = hooks._format_gap_since_last(_iso(last), now=now)
    assert "# gap_since_last:" in out
    assert "4h" in out
    assert "long quiet" not in out  # soft band, no strong-signal text


def test_gap_long_band_over_24h():
    """>24h → strong line with the explicit voice-line directive."""
    now = datetime(2026, 5, 21, 14, 0, tzinfo=UTC)
    last = now - timedelta(days=2, hours=3)
    out = hooks._format_gap_since_last(_iso(last), now=now)
    assert "# gap_since_last:" in out
    assert "2d" in out
    assert "you went quiet" in out  # references the voice line


def test_gap_unparseable_ts_returns_empty():
    """Garbage in runtime_state → no injection, no crash."""
    out = hooks._format_gap_since_last("not-a-timestamp")
    assert out == ""


def test_gap_missing_ts_returns_empty():
    """No runtime_state row → no injection."""
    out = hooks._format_gap_since_last(None)
    assert out == ""
```

- [ ] **Step B.1.2:** Run — confirm fail (function doesn't exist).

Run: `uv run pytest tests/test_gap_awareness.py -xvs`
Expected: `AttributeError: module 'agents.hooks' has no attribute '_format_gap_since_last'`.

### Task B.2: Implement `_format_gap_since_last`

**Files:**
- Modify: `agents/hooks.py` — add helper near the other `_format_*` helpers (the file has them grouped around lines 62-330; pick the appropriate insertion point near `_format_core_blocks`).

- [ ] **Step B.2.1:** Add the helper.

```python
def _format_gap_since_last(
    last_user_message_iso: str | None,
    *,
    now: datetime | None = None,
) -> str:
    """Format a # gap_since_last: line based on how long since the last
    user message. Returns "" if invisible (<2h) or unparseable. Two bands:
    2h-24h soft, >24h strong (triggers the existing voice line).

    Thresholds are config-driven via gap_awareness.{soft,long}_threshold_hours.
    """
    if not last_user_message_iso:
        return ""
    try:
        last = datetime.fromisoformat(last_user_message_iso)
    except (TypeError, ValueError):
        return ""
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    if now is None:
        now = datetime.now(UTC)
    if not cfg.get("gap_awareness.enabled", True):
        return ""
    soft_h = float(cfg.get("gap_awareness.soft_threshold_hours", 2))
    long_h = float(cfg.get("gap_awareness.long_threshold_hours", 24))
    delta = now - last
    total_h = delta.total_seconds() / 3600.0
    if total_h < soft_h:
        return ""
    if total_h < long_h:
        return f"# gap_since_last: {int(round(total_h))}h"
    days = int(delta.total_seconds() // 86400)
    return (
        f"# gap_since_last: {days}d (long quiet — your "
        f'"you went quiet. that\'s disruptive" rule applies)'
    )
```

`agents/hooks.py` already imports `from datetime import UTC, datetime` and `from . import config as cfg` (verified). The `timedelta` import will need to be added if not already present.

- [ ] **Step B.2.2:** Wire it into `inject_memory`. Insert immediately after `_format_now(...)` (line 325 area):

```python
# After: parts.append(_format_now(...))
last_msg = db.runtime_get("last_user_message")
gap_block = _format_gap_since_last(last_msg)
if gap_block:
    parts.append(gap_block)
```

- [ ] **Step B.2.3:** Run formatter tests — confirm pass.

Run: `uv run pytest tests/test_gap_awareness.py -xvs`

### Task B.3: Integration test through inject_memory

- [ ] **Step B.3.1:** Add an integration test that exercises the hook end-to-end.

```python
import importlib
from pathlib import Path


@pytest.fixture
def _isolated_db(tmp_path: Path, monkeypatch):
    """Per-test fresh DB. Mirrors tests/test_facts_recall_decay.py:23-39."""
    from storage import db as _db
    db_path = tmp_path / "hikari.db"
    monkeypatch.setenv("HIKARI_DB_PATH", str(db_path))
    importlib.reload(_db)
    monkeypatch.setattr(_db, "_DB_PATH", db_path)
    _db._reset_schema_sentinel()
    yield _db


def test_inject_memory_emits_gap_block_when_long_quiet(_isolated_db):
    """End-to-end: stale last_user_message in runtime_state → inject_memory
    output contains the gap_since_last line."""
    db = _isolated_db
    stale = datetime.now(UTC) - timedelta(days=3)
    db.runtime_set("last_user_message", _iso(stale))

    import asyncio
    out = asyncio.run(hooks.inject_memory({"prompt": "test"}))
    text = out.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "# gap_since_last:" in text
    assert "3d" in text


def test_inject_memory_omits_gap_block_when_fresh(_isolated_db):
    """End-to-end: fresh last_user_message → no gap_since_last line."""
    db = _isolated_db
    fresh = datetime.now(UTC) - timedelta(minutes=15)
    db.runtime_set("last_user_message", _iso(fresh))
    import asyncio
    out = asyncio.run(hooks.inject_memory({"prompt": "test"}))
    text = out.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "# gap_since_last:" not in text
```

- [ ] **Step B.3.2:** Run — confirm pass.

Run: `uv run pytest tests/test_gap_awareness.py -xvs`

### Task B.4: Add config block

**Files:**
- Modify: `config/engagement.yaml`

- [ ] **Step B.4.1:** Add the block (location: somewhere near `proactive:` since these are conceptually adjacent).

```yaml
# --- Gap awareness ---
# Inject `# gap_since_last:` block into inject_memory when the user
# has been quiet. Three bands: <soft_threshold_hours invisible,
# soft_threshold_hours..long_threshold_hours soft notice, >long
# triggers the explicit voice-line directive. Mirrors the existing
# re-engagement bands so two systems don't drift apart.
gap_awareness:
  enabled: true
  soft_threshold_hours: 2
  long_threshold_hours: 24
```

### Task B.5: Full suite + commit

- [ ] **Step B.5.1:** Run the full suite.

Run: `uv run pytest -q`

- [ ] **Step B.5.2:** Commit.

```bash
git add agents/hooks.py config/engagement.yaml tests/test_gap_awareness.py
git commit -m "feat(hooks): gap-since-last block in inject_memory

Reads runtime_state.last_user_message (already populated by chat/photo/
voice/image/start handlers) and injects '# gap_since_last:' when ≥2h
elapsed. Three bands: <2h invisible / 2-24h soft / >24h strong (the
strong band references her existing 'you went quiet. that's
disruptive' voice line from CLAUDE.md, so the directive is in-context).

Config-driven thresholds under gap_awareness.* in engagement.yaml.
Mirrors the existing re-engagement bands so two systems don't drift."
```

**Feature B success criteria:**
- ✅ `_format_gap_since_last` returns correct band by elapsed time
- ✅ Empty string when invisible (<2h) or unparseable
- ✅ `inject_memory` emits the block end-to-end
- ✅ Config-driven thresholds respected
- ✅ full pytest green
- ✅ Runtime verify: launchd restart + send a Telegram message → no block should appear (fresh). Wait until you have time, then verify gap injection appears in next inject_memory cycle via debug logs or a manual `db.runtime_set("last_user_message", "2026-05-19T...")` poke before a turn.

---

# Feature C: Apple Shortcuts MCP

**Why:** One bridge — every Shortcut the user authors becomes a tool. Effort multiplier vs. building per-app MCPs. Same `npx -y` + TCC ritual as `apple_events`. No env, no auth. Local trusted output.

**Architecture:** Three lines in `.mcp.json` + one line in the allowlist + README mention.

### Task C.1: Failing test

**Files:**
- Create: `tests/test_apple_shortcuts_mcp.py`

- [ ] **Step C.1.1:** Write tests.

```python
"""Apple Shortcuts MCP wiring smoke tests."""
from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_apple_shortcuts_in_mcp_json():
    """`.mcp.json` has an `apple_shortcuts` server entry using npx."""
    config = json.loads((REPO_ROOT / ".mcp.json").read_text())
    assert "apple_shortcuts" in config["mcpServers"]
    entry = config["mcpServers"]["apple_shortcuts"]
    assert entry["command"] == "npx"
    assert "mcp-server-apple-shortcuts" in " ".join(entry["args"])


def test_apple_shortcuts_in_allowlist():
    """runtime.py allowlist contains mcp__apple_shortcuts__*."""
    src = (REPO_ROOT / "agents" / "runtime.py").read_text()
    assert "mcp__apple_shortcuts__*" in src
```

- [ ] **Step C.1.2:** Run — confirm fail.

Run: `uv run pytest tests/test_apple_shortcuts_mcp.py -xvs`

### Task C.2: Wire it up

**Files:**
- Modify: `.mcp.json`
- Modify: `agents/runtime.py` (line 200-205 area)
- Modify: `README.md` (macOS native integrations section)

- [ ] **Step C.2.1:** Add to `.mcp.json` `mcpServers` (alongside `apple_events`):

```json
"apple_shortcuts": {
  "command": "npx",
  "args": ["-y", "mcp-server-apple-shortcuts"]
}
```

- [ ] **Step C.2.2:** Add to the allowlist in `agents/runtime.py:200-205`:

```python
"mcp__apple_shortcuts__*",
```

- [ ] **Step C.2.3:** Add a README bullet near the existing Apple Reminders / Calendar / Notes section:

```markdown
- **Apple Shortcuts** (via the `apple_shortcuts` MCP server): exposes every Shortcut you've authored in the Shortcuts app as a callable tool. First call may trigger an Automation permission prompt — accept in System Settings → Privacy & Security → Automation. No env vars or auth needed.
```

- [ ] **Step C.2.4:** Run — confirm pass.

Run: `uv run pytest tests/test_apple_shortcuts_mcp.py -xvs`

### Task C.3: Full suite + commit

- [ ] **Step C.3.1:** Run full suite.

Run: `uv run pytest -q`

- [ ] **Step C.3.2:** Commit.

```bash
git add .mcp.json agents/runtime.py README.md tests/test_apple_shortcuts_mcp.py
git commit -m "feat(mcp): wire Apple Shortcuts MCP

Exposes every Shortcut you've authored in the Shortcuts app as a
callable tool via recursechat/mcp-server-apple-shortcuts. Same
npx + TCC ritual as apple_events; no env vars, no auth.

Effort multiplier — every iOS automation you build becomes a tool
without writing an MCP per app (Focus modes, HomeKit, Things,
Drafts, Bear, Streaks, Day One, ...)."
```

**Feature C success criteria:**
- ✅ `.mcp.json` parses, entry present
- ✅ Allowlist contains wildcard
- ✅ launchd restart clean; first `mcp__apple_shortcuts__list_shortcuts` call works (manual verify after launchctl restart — Hikari should be able to list Shortcuts when you ask)

---

# Feature D: YouTube Transcript MCP

**Why:** Drop a 90-min interview link, get a summary. No auth, no env. Mirrors Apple Shortcuts pattern but adds a `prompt_injection.wrap_patterns` entry because transcript content is external/untrusted (videos can say anything, including prompt-injection attempts).

### Task D.1: Failing test

**Files:**
- Create: `tests/test_youtube_transcript_mcp.py`

- [ ] **Step D.1.1:** Write tests.

```python
"""YouTube Transcript MCP wiring smoke tests."""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_youtube_transcript_in_mcp_json():
    """`.mcp.json` has a `youtube_transcript` server entry using npx."""
    config = json.loads((REPO_ROOT / ".mcp.json").read_text())
    assert "youtube_transcript" in config["mcpServers"]
    entry = config["mcpServers"]["youtube_transcript"]
    assert entry["command"] == "npx"
    assert "mcp-youtube-transcript" in " ".join(entry["args"])


def test_youtube_transcript_in_allowlist():
    """runtime.py allowlist contains mcp__youtube_transcript__*."""
    src = (REPO_ROOT / "agents" / "runtime.py").read_text()
    assert "mcp__youtube_transcript__*" in src


def test_youtube_transcript_in_wrap_patterns():
    """engagement.yaml prompt_injection.wrap_patterns contains a regex
    matching mcp__youtube_transcript__* — transcript content is external."""
    cfg = yaml.safe_load((REPO_ROOT / "config" / "engagement.yaml").read_text())
    patterns = cfg["prompt_injection"]["wrap_patterns"]
    matched = any(
        re.match(pat, "mcp__youtube_transcript__get_transcript")
        for pat in patterns
    )
    assert matched, f"no wrap_pattern matched mcp__youtube_transcript__*; patterns: {patterns}"
```

- [ ] **Step D.1.2:** Run — confirm fail.

Run: `uv run pytest tests/test_youtube_transcript_mcp.py -xvs`

### Task D.2: Wire it up

**Files:**
- Modify: `.mcp.json`
- Modify: `agents/runtime.py` (allowlist)
- Modify: `config/engagement.yaml` (wrap_patterns)

- [ ] **Step D.2.1:** Add to `.mcp.json` `mcpServers`:

```json
"youtube_transcript": {
  "command": "npx",
  "args": ["-y", "mcp-youtube-transcript"]
}
```

- [ ] **Step D.2.2:** Add to the allowlist:

```python
"mcp__youtube_transcript__*",
```

- [ ] **Step D.2.3:** Add to `config/engagement.yaml` `prompt_injection.wrap_patterns` (line 727-755 area). Follow the existing pattern style (regex anchored at `^`).

```yaml
  - "^mcp__youtube_transcript__"
```

- [ ] **Step D.2.4:** Run — confirm pass.

Run: `uv run pytest tests/test_youtube_transcript_mcp.py -xvs`

### Task D.3: Full suite + commit

- [ ] **Step D.3.1:** Run full suite.

Run: `uv run pytest -q`

- [ ] **Step D.3.2:** Commit.

```bash
git add .mcp.json agents/runtime.py config/engagement.yaml tests/test_youtube_transcript_mcp.py
git commit -m "feat(mcp): wire YouTube Transcript MCP

Adds jkawamoto/mcp-youtube-transcript via npx. No auth, no env.
Allowlist + prompt_injection.wrap_patterns (transcript content is
external/untrusted — videos can say anything, including injection
attempts). Mirrors the google_workspace/notion external-wrap pattern."
```

**Feature D success criteria:**
- ✅ `.mcp.json` parses, entry present
- ✅ Allowlist contains wildcard
- ✅ wrap_patterns regex matches `mcp__youtube_transcript__*`
- ✅ launchd restart clean

---

# Phase Final: Wiki update + push

- [ ] Wiki: Per global CLAUDE.md, update alt-wiki if this involved web research / new patterns. This batch is mostly wiring + a small additive schema change; no novel pattern emerged. Skip unless something surprising came up during implementation.
- [ ] Push: `git push origin main` (ship_method=push per CLAUDE.md Ship profile)

**Total estimated time:** 4-6 hours focused work, including TDD per feature, full-suite runs, and 4 launchd restart verifications.
