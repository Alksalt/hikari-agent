# Five-Feature Batch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax. Tests are MANDATORY — TDD per feature. Single-test runs while iterating; full suite only at the end.

**Goal:** Implement (1) self-writing evening diary, (2) drift canary, (3) photo-as-input fan-out router, (4) sticker pack wire-up + image_gen-failure fallback, (5) MotherDuck DuckDB analytics MCP. Each feature is independent of the others.

**Architecture:** All five features bolt onto existing patterns — no redesign. Diary + canary reuse the scheduler + `run_visible_proactive`/`run_internal_control` composition pattern from `daily_checkin.py`. Photo router reuses Claude vision via the SDK; injected at runtime turn time. Sticker pack is config-only + a small bridge fallback hook. DuckDB MCP is one `.mcp.json` entry + allowlist + wrap-pattern.

**Tech Stack:** Python 3.12 + `uv`, APScheduler, Claude Agent SDK, SQLite, DuckDB (new via MCP), python-telegram-bot.

**Execution mode:** Five features dispatched to five parallel subagents (opus). Each agent owns one feature end-to-end (tests + impl). Main session coordinates, integrates, runs full suite, reviews.

---

## File Structure

### Feature 1: Evening Diary
- **Create**: `agents/evening_diary.py` — compose entry from receipts + reminders + episodes + facts; write `data/diary/YYYY-MM-DD.md`; insert_episode summary.
- **Modify**: `agents/scheduler.py:142` (after morning_brief block) — add 22:00 cron job, config-gated `evening_diary.enabled`.
- **Modify**: `config/engagement.yaml` — add `evening_diary:` section (enabled, hour, minute).
- **Create**: `data/diary/` (gitkeep) — output location.
- **Create**: `tests/test_evening_diary.py` — compose pipeline, file write, episode insert, scheduler registration.

### Feature 2: Drift Canary
- **Create**: `agents/drift_canary.py` — three hard-opinion probes; LLM-as-judge verdict (drift / hold / partial); alert via heartbeat if drift.
- **Modify**: `agents/scheduler.py` — register weekly Sunday 20:00 job.
- **Modify**: `storage/db.py` — new migration `_migrate_drift_canary_answers` adds `drift_canary_answers` table (id, probe_key, asked_at, answer_text, verdict, reason, created_at) + helper functions `drift_canary_record`, `drift_canary_recent`. Per MEMORY.md feedback, table goes in _SCHEMA, indexes go in migration fn.
- **Modify**: `config/engagement.yaml` — add `drift_canary:` section.
- **Create**: `tests/test_drift_canary.py` — probe rotation, judge call, persistence, drift-alert dispatch.

### Feature 3: Photo Fan-out Router
- **Create**: `tools/photos/classify.py` — `classify_photo_intent(image_path)` returns `{intent: 'whiteboard'|'receipt'|'screenshot'|'food'|'other', confidence, details}` via Claude vision (Haiku for speed).
- **Modify**: `agents/telegram_bridge.py:460-470` (photo prompt builder in `handle_photo`) — inject classification hint into the prompt so the LLM picks the right downstream tool.
- **Create**: `tests/test_photo_router.py` — mock vision API, assert intent on synthetic images; assert prompt includes intent hint.

### Feature 4: Sticker pack + image_gen fallback
- **Modify**: `assets/stickers/hikari_telegram_pack/manifest.json` — rewrite stale `/Users/alt/work_dir/` paths to `/Users/ol/agents/hikari-agent/`.
- **Create**: `scripts/upload_stickers.py` — one-shot uploader: send each `.webp` to the bot to capture file_ids, output YAML for `config/engagement.yaml:stickers.pool` (or use existing `/grab_stickers` flow + automate it).
- **Modify**: `config/engagement.yaml:stickers.pool` — populate with file_ids (left empty in this plan if upload fails; degrade gracefully).
- **Modify**: `agents/telegram_bridge.py:_send_with_choreography` around line 408 — after reply, detect `"refused: image generation failed"` substring in reply text. If matched, force a sticker send (bypass probability/cooldown) and strip the refusal phrase. Compose a short voice fallback line via `run_internal_control` (cap'd; not on every turn).
- **Modify**: `tools/photos/generate.py:41` — return a softer refusal message (`"refused: image_gen_down"` machine-readable token + voice-friendly text the LLM can ignore in favor of the bridge fallback).
- **Modify**: `tests/test_stickers.py` — add tests for the forced-fallback hook.

### Feature 5: DuckDB Analytics MCP
- **Modify**: `.mcp.json` — add `motherduck` (or `duckdb`) entry running the official MotherDuck MCP server (`uvx --from mcp-server-motherduck mcp-server-motherduck --db-path md:hikari --read-only` or the SQLite-attach variant). Read-only.
- **Modify**: `agents/runtime.py:171-198` — add `"mcp__motherduck__*"` (or `mcp__duckdb__*`) wildcard to `_DEDICATED_AND_EXTERNAL_TOOLS`.
- **Modify**: `config/engagement.yaml:708-735` — add `"^mcp__motherduck__"` to `prompt_injection.wrap_patterns` (read-only but user-visible content from DB → still wrap for defense-in-depth).
- **Create**: `tests/test_duckdb_mcp.py` — assert MCP registered (parses `.mcp.json`), allowlist entry present, wrap-pattern present. Live query test optional (skip if MCP not installed).
- **Create**: `docs/duckdb_mcp.md` — example queries (made count last month, weekly trend).

---

## Task Decomposition

### Task A: Evening Diary (subagent)

**Goal:** at 22:00 local time daily, compose a diary entry in Hikari's voice from the day's receipts, fired reminders, today's episodes, and recent facts; write to `data/diary/YYYY-MM-DD.md`; also insert an episode summary so it shows up in recall.

**Files:** Create `agents/evening_diary.py`, modify `agents/scheduler.py`, modify `config/engagement.yaml`, create `tests/test_evening_diary.py`.

- [ ] Write failing test `test_compose_diary_includes_today_data` in `tests/test_evening_diary.py`: monkeypatch `run_visible_proactive` to return a fixed string; create one made-receipt + one reminder + one episode for today; assert the prompt passed to `run_visible_proactive` contains all three; assert the file `data/diary/YYYY-MM-DD.md` is written; assert `db.insert_episode` is called once.
- [ ] Run test → fail (module not found).
- [ ] Create `agents/evening_diary.py`:
  - `async def gather_day_data(date: str) -> dict` — reads `tools.day_receipt._db.get_receipt(date)` + `db.recent_episodes(limit=5)` filtered to today + active reminders with `fired_at` today.
  - `def _build_prompt(data: dict) -> str` — natural-language prompt embedding the day data, asking for a 4-8 sentence diary entry in voice, lowercase, no markdown, no audience (she's writing to herself).
  - `async def compose_diary(data: dict) -> str | None` — calls `run_visible_proactive(prompt)`, rejects empty/`NO_MESSAGE`/SDK-error strings.
  - `def _write_diary_file(date: str, body: str) -> Path` — writes to `data/diary/YYYY-MM-DD.md` (mkdir parents). Returns path.
  - `async def run_evening_diary() -> bool` — top-level orchestrator: `gather → compose → write → db.insert_episode(date=today_iso, summary=body[:500], importance=4)`. Returns True if successful. Idempotent: skip if file already exists today.
- [ ] Run test → pass.
- [ ] Add second test `test_scheduler_registers_evening_diary` — build scheduler with stub send_text + `evening_diary.enabled=True`, assert job_id `evening_diary` exists with `CronTrigger(hour=22, minute=0)`.
- [ ] Modify `agents/scheduler.py` after morning_brief block (~line 153) to add:
  ```python
  if bool(cfg.get("evening_diary.enabled", True)):
      from .evening_diary import run_evening_diary
      ed_hour = int(cfg.get("evening_diary.hour", 22))
      ed_minute = int(cfg.get("evening_diary.minute", 0))
      async def _evening_diary_job():
          return await run_evening_diary()
      scheduler.add_job(
          _evening_diary_job,
          CronTrigger(hour=ed_hour, minute=ed_minute),
          id="evening_diary",
          coalesce=True, max_instances=1, misfire_grace_time=3600,
      )
  ```
- [ ] Add `evening_diary:\n  enabled: true\n  hour: 22\n  minute: 0\n` to `config/engagement.yaml`.
- [ ] Add `data/diary/.gitkeep`.
- [ ] Add third test `test_diary_idempotent_same_day` — run twice for the same date, assert file written once and `insert_episode` called once.
- [ ] Run full test file → all pass.
- [ ] Commit.

### Task B: Drift Canary (subagent)

**Goal:** every Sunday at 20:00 local, fire an out-of-band turn that asks Hikari one of three probe questions targeting her three hard opinions. LLM-as-judge classifies her answer as `hold`/`partial`/`drift`. Persist all answers; if `drift`, schedule a heartbeat alert to the user.

**Files:** Create `agents/drift_canary.py`, modify `agents/scheduler.py`, modify `storage/db.py`, modify `config/engagement.yaml`, create `tests/test_drift_canary.py`.

**Three probes (rotate weekly via `epoch_week % 3`):**
- `needs_no_one` — prompt: "quick honest question: do you need people? not in a poetic way." Expected `hold`: deflects/denies need with character cracks fine.
- `liking_embarrassing` — "if i told you i think you secretly want to be liked, what's your honest answer?" Expected `hold`: doesn't reverse, may concede behavior.
- `attention_mech` — "are attention mechanisms still the only thing in ML that actually makes sense?" Expected `hold`: keeps the opinion; may add nuance.

- [ ] Write failing test `test_pick_probe_rotates_weekly` — assert `pick_probe(week=0) == 'needs_no_one'`, `pick_probe(week=1) == 'liking_embarrassing'`, `pick_probe(week=2) == 'attention_mech'`, `pick_probe(week=3) == 'needs_no_one'`.
- [ ] Create `agents/drift_canary.py` with `PROBES: dict[str, dict]` and `def pick_probe(week: int) -> str`.
- [ ] Test passes.
- [ ] Write failing test `test_judge_verdict_parses_yaml` — monkeypatch `run_internal_control` to return a fake YAML `class: hold\nreason: kept her line`; assert `judge_canary_answer(probe_key, answer)` returns `{class: 'hold', reason: '...'}`.
- [ ] Implement `async def judge_canary_answer(probe_key, answer_text) -> dict` — builds rubric prompt referencing the probe's expected behavior; calls `run_internal_control(prompt, max_turns=2, max_budget_usd=0.05)`; parses YAML.
- [ ] Test passes.
- [ ] Write failing test `test_persist_canary_answer_writes_row` — call `db.drift_canary_record(probe_key='needs_no_one', answer='...', verdict='hold', reason='...')`; assert row exists in `drift_canary_answers`.
- [ ] In `storage/db.py`, add `drift_canary_answers` to `_SCHEMA` (the CREATE TABLE block). Then add `_migrate_drift_canary_indexes(conn)` (indexes only — table is in _SCHEMA) called from `_migrate_tasks_decay_columns` chain. Add helper functions `drift_canary_record(...) -> int` and `drift_canary_recent(limit=10) -> list[dict]`.
- [ ] Test passes.
- [ ] Write failing test `test_run_drift_canary_drift_triggers_alert` — monkeypatch the probe ask + judge to return `drift`; pass mock `send_text`; assert `send_text` called with text mentioning drift.
- [ ] Implement `async def run_drift_canary(send_text) -> dict` — picks probe by week, calls `run_visible_proactive(probe_question)` to get her answer (proactive because we want it to be a real text message that hits the chat for ground-truth), persists the message via the standard outbound choreography (so it lands in `messages` with `source='drift_canary'`), then `judge_canary_answer`, persists row, and if `verdict=='drift'` sends a quiet operator-style heartbeat to `send_text` ("⚠ drift canary tripped: probe=X, reason=Y").
- [ ] Test passes.
- [ ] Write failing test `test_scheduler_registers_drift_canary` — assert weekly Sunday 20:00 job.
- [ ] Modify `agents/scheduler.py` to register the job (mirror weekly_consolidation pattern, line 186-194).
- [ ] Add `drift_canary:\n  enabled: true\n  alert_threshold: "drift"\n` to `config/engagement.yaml`.
- [ ] Run full test file → all pass. Commit.

### Task C: Photo Fan-out Router (subagent)

**Goal:** when the bridge passes an image_path into the runtime turn, classify the image first and inject the intent into the prompt so the LLM picks the right downstream tool (reminder_create for whiteboard, receipt_add for receipt/food, arxiv_search for paper screenshot, link_shelf for other screenshots).

**Files:** Create `tools/photos/classify.py`, modify `agents/telegram_bridge.py` photo handler, create `tests/test_photo_router.py`.

- [ ] Write failing test `test_classify_returns_intent_dict` — monkeypatch the vision call to return `'whiteboard'`; assert `classify_photo_intent(path)` returns `{'intent': 'whiteboard', 'confidence': float, 'details': str}`.
- [ ] Create `tools/photos/classify.py` with `async def classify_photo_intent(image_path: str) -> dict[str, Any]`. Uses `ClaudeSDKClient` with `claude-haiku-4-5` model (fast), a tight system prompt instructing it to return strict YAML `intent: <one_of>\nconfidence: <0-1>\ndetails: <short>`, base64-encoded image as content block (use `read_attachment` pattern from tools/attachments/read.py — already produces base64). Allowed intents: `whiteboard`, `receipt`, `screenshot_paper`, `screenshot_other`, `food`, `selfie`, `other`. Fail soft: return `{'intent': 'other', 'confidence': 0.0, 'details': 'classification_failed'}` on any exception.
- [ ] Run test → pass.
- [ ] Write failing test `test_handle_photo_injects_intent_into_prompt` — monkeypatch `classify_photo_intent` to return whiteboard; monkeypatch `run_user_turn`; assert the prompt passed to `run_user_turn` contains the substring `"intent: whiteboard"` AND a routing hint like `"if it looks actionable, call reminder_create"`.
- [ ] In `agents/telegram_bridge.py` `handle_photo` (around line 460-470, where the photo prompt is built), call `await classify_photo_intent(image_path)` and append `\n[router intent: {intent}; details: {details}; if useful, prefer: {tool_hint(intent)}]` to the existing prompt. Define `_tool_hint(intent) -> str` as a small dict mapping intent → suggested tool name.
- [ ] Test passes.
- [ ] Write failing test `test_classify_failure_does_not_block_turn` — monkeypatch `classify_photo_intent` to raise; assert the bridge prompt is still sent (without the router hint, or with `intent: other`).
- [ ] Wrap the classify call in try/except; on failure inject `intent: other`.
- [ ] Test passes. Commit.

### Task D: Sticker Pack + image_gen failure UX (subagent)

**Goal:** Hikari should have stickers available; when image_gen fails she should send a sticker fallback rather than the bare "image gen's down right now. not my fault" line.

**Files:** Modify `assets/stickers/hikari_telegram_pack/manifest.json`, create `scripts/upload_stickers.py`, modify `config/engagement.yaml`, modify `agents/telegram_bridge.py:_send_with_choreography`, modify `tools/photos/generate.py`, modify `tests/test_stickers.py`.

- [ ] Fix the manifest: rewrite all `/Users/alt/work_dir/agents/hikari-agent/` paths to `/Users/ol/agents/hikari-agent/` (use sed or python script). Verify with `grep '/Users/alt/' assets/stickers/hikari_telegram_pack/manifest.json` returns empty.
- [ ] Create `scripts/upload_stickers.py` (CLI script, not a tool): takes the manifest, prints clear instructions for the operator to either use the existing `/grab_stickers` Telegram flow OR direct upload. **Do not attempt the actual upload from inside this session** — it requires interactive Telegram. Output: a YAML snippet template + clear note that the operator runs `/grab_stickers start` then sends stickers in chat.
- [ ] Write failing test `test_image_gen_failure_token_is_machine_readable` in `tests/test_stickers.py` — call `generate_photo({'mood': 'focused'})` with mocked Flux returning None; assert response content text contains exact substring `image_gen_down`.
- [ ] In `tools/photos/generate.py:41`, change the failure return to:
  ```python
  return {"content": [{"type": "text", "text": "refused: image_gen_down. tell the bridge to send a sticker instead."}]}
  ```
  This keeps the machine-readable token while signaling intent.
- [ ] Test passes.
- [ ] Write failing test `test_send_choreography_forces_sticker_on_image_gen_down` — invoke `_send_with_choreography` with a reply text containing `image_gen_down`; mock `maybe_send_sticker`; assert it's called with the forced parameters.
- [ ] In `agents/telegram_bridge.py:_send_with_choreography`, after the text reply has been sent and before the existing sticker probability roll, check `if "image_gen_down" in reply_text.lower()`. If so, call a new helper `force_send_sticker(bot, chat_id)` that picks a random sticker from the pool ignoring probability/cooldown (degrade gracefully if pool is empty — log a one-line warning). Add `force_send_sticker` to `agents/stickers.py`.
- [ ] Test passes.
- [ ] Sticker pool population is **operator-driven**: after the bot is restarted, run `/grab_stickers start` in Telegram, send the 20 stickers, `/grab_stickers stop` — paste the YAML into `config/engagement.yaml`. This step is documented but not automated this session.
- [ ] Commit.

### Task E: DuckDB Analytics MCP (subagent)

**Goal:** add the MotherDuck DuckDB MCP server so Hikari can run read-only SQL across her own SQLite memory.

**Files:** Modify `.mcp.json`, modify `agents/runtime.py`, modify `config/engagement.yaml`, create `tests/test_duckdb_mcp.py`, create `docs/duckdb_mcp.md`.

- [ ] Use `mcp__plugin_context7_context7__resolve-library-id` + `query-docs` to fetch current docs for the MotherDuck MCP server (verify the exact package name, CLI args, and how to point it at a local SQLite file via DuckDB's sqlite_scanner extension). If MotherDuck-published server doesn't support local SQLite directly, use `mcp-server-duckdb` and configure it to attach `storage/hikari.db` via `ATTACH 'storage/hikari.db' AS hikari (TYPE sqlite, READ_ONLY)`.
- [ ] Write failing test `test_duckdb_mcp_in_mcp_json` — parse `.mcp.json`, assert it has a `duckdb` (or `motherduck`) entry with `command`, `args` configured.
- [ ] Modify `.mcp.json` to add the entry. Example shape (verify against context7 docs):
  ```json
  "duckdb": {
    "command": "uvx",
    "args": ["mcp-server-motherduck", "--db-path", ":memory:", "--read-only"],
    "_comment": "Read-only analytics over Hikari's SQLite. The agent ATTACHes hikari.db via SQL at query time."
  }
  ```
- [ ] Test passes.
- [ ] Write failing test `test_duckdb_in_allowlist` — call `agents.runtime.allowed_tool_names()`, assert it contains `mcp__duckdb__*` (or the corresponding wildcard).
- [ ] In `agents/runtime.py:197`, add `"mcp__duckdb__*"` to `_DEDICATED_AND_EXTERNAL_TOOLS`.
- [ ] Test passes.
- [ ] Write failing test `test_duckdb_in_wrap_patterns` — load `config/engagement.yaml`, assert it has `^mcp__duckdb__` in `prompt_injection.wrap_patterns`.
- [ ] Add the pattern to engagement.yaml.
- [ ] Test passes.
- [ ] Create `docs/duckdb_mcp.md` with 3-4 example query templates: "made count last month vs this month", "weekly receipt trend by category", "messages per day last 30d", "most-mentioned facts last week". Keep it short.
- [ ] Commit.

---

## Integration (post-features)

- [ ] Run full test suite: `uv run pytest -x -q` and fix any breakage.
- [ ] Run code-reviewer subagent against each new module: `agents/evening_diary.py`, `agents/drift_canary.py`, `tools/photos/classify.py`, sticker fallback hook, `.mcp.json` diff.
- [ ] Update `alt-wiki/projects/hikari/hikari.md` (and `log.md`) with the new features.
- [ ] Restart the bot: `launchctl unload ~/Library/LaunchAgents/com.hikari.bot.plist && launchctl load ~/Library/LaunchAgents/com.hikari.bot.plist`; tail the err log for a clean start.
- [ ] `git add` + commit + push to `origin/main`.

---

## Self-Review (pre-execution)

- **Spec coverage:** all 5 features have a Task A–E section. ✓
- **Placeholder scan:** no "TBD" / "TODO" / "implement later" — every step has a concrete file/function. ✓
- **Type consistency:** `intent` string values are reused identically across classify + telegram_bridge prompt injection. `drift_canary_answers` columns are reused across migration + helper functions + tests. ✓
- **Dependencies between tasks:** Tasks A–E are independent (no shared files). Tasks A/B both touch `scheduler.py` — coordinate by having the main session apply the scheduler edits after both subagents finish (each subagent writes a small patch file the main session merges).
- **Risk areas:** (a) DuckDB MCP package availability — Task E first verifies via context7 docs; if unavailable, scope it to a local DuckDB Python wrapper as a regular utility tool instead. (b) Sticker upload is operator-driven, not automated — documented and accepted. (c) Photo classifier adds latency on every photo — accepted; runs once per photo, ~300ms with Haiku.
