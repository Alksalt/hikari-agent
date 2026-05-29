"""Background research worker — runs scheduled, processes tasks with
research_intent=1 that haven't been researched yet.

Spawns a fresh ClaudeSDKClient per task with WebSearch + WebFetch.
Writes research_summary back to the tasks table. Daily cap.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


def _today() -> str:
    return date.today().isoformat()


def _daily_loops_today() -> int:
    from storage import db
    if db.runtime_get("research_worker.loops_today_date") != _today():
        return 0
    return db.runtime_get_int("research_worker.loops_today", 0)


def _bump_loops_today() -> None:
    from storage import db
    if db.runtime_get("research_worker.loops_today_date") != _today():
        db.runtime_set("research_worker.loops_today_date", _today())
        db.runtime_set("research_worker.loops_today", 1)
    else:
        db.runtime_set("research_worker.loops_today", _daily_loops_today() + 1)


def _pending_tasks(limit: int, max_age_days: int) -> list[dict]:
    from storage import db
    cutoff = (datetime.now(UTC) - timedelta(days=max_age_days)).isoformat()
    with db._conn() as c:
        rows = c.execute(
            """
            SELECT id, subject, description, created_at
            FROM tasks
            WHERE research_intent = 1
              AND research_summary IS NULL
              AND status IN ('open', 'pending')
              AND created_at >= ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (cutoff, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


async def _research_one(task: dict) -> tuple[str, list[str]] | None:
    """Spawn an SDK session to research one task. Returns (summary, sources) or None."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        TextBlock,
    )
    from agents import config as cfg

    subject = task.get("subject", "")[:200]
    description = (task.get("description") or "")[:500]
    query = (
        f"Research: {subject}\n\n"
        f"Context: {description}\n\n"
        f"Return a 2-3 sentence summary plus source URLs (max 5)."
    )

    research_prompt_path = Path(__file__).parent / "prompts" / "research.prompt.md"
    if research_prompt_path.exists():
        system = research_prompt_path.read_text(encoding="utf-8")
    else:
        system = (
            "You are a research assistant. Use WebSearch and WebFetch to find current "
            "information. Return a concise summary with source URLs."
        )

    max_turns = int(cfg.get("research_worker.per_task_max_turns", 8))
    max_budget_usd = float(cfg.get("research_worker.per_task_max_budget_usd", 0.50))
    options = ClaudeAgentOptions(
        system_prompt=system,
        allowed_tools=["WebSearch", "WebFetch"],
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        permission_mode="default",
        setting_sources=["project"],
    )

    parts: list[str] = []
    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(query)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            parts.append(block.text)
                if isinstance(msg, ResultMessage):
                    try:
                        from agents.runtime import _record_llm_cost
                        _record_llm_cost(
                            getattr(msg, "model_usage", None),
                            path="research_worker",
                            fallback_model="claude-sonnet-4-6",
                            fallback_usage=getattr(msg, "usage", None),
                        )
                    except Exception:
                        logger.debug("research_worker cost log failed", exc_info=True)
                    break
    except Exception:
        logger.exception("research_worker: SDK session failed for task %s", task.get("id"))
        return None

    if not parts:
        return None
    summary = " ".join(parts).strip()
    # Extract URLs.
    urls = re.findall(r"https?://[\w\-./?=&%#]+", summary)
    urls = [u for u in urls if len(u) < 500][:5]
    return summary[:2000], urls


def _sanitize_summary(text: str) -> str:
    """Defense against prompt injection in research output."""
    try:
        from agents.reflection_sanitize import MemoryInstructionShape, sanitize
        try:
            sanitize(text, kind="observation")
        except MemoryInstructionShape:
            return "(research output rejected by sanitizer)"
    except Exception:
        pass
    return text


async def run_research_worker() -> int:
    """Entry point called by scheduler. Returns count of tasks processed."""
    from agents import config as cfg
    from storage import db

    if not bool(cfg.get("research_worker.enabled", True)):
        return 0

    # Skip when main runtime lock is held (busy chat).
    try:
        from agents.runtime import _RUN_LOCK
        if _RUN_LOCK.locked():
            logger.debug("research_worker: skipped — _RUN_LOCK held")
            return 0
    except Exception:
        pass

    max_loops = int(cfg.get("research_worker.max_loops_per_day", 2))
    if _daily_loops_today() >= max_loops:
        logger.debug("research_worker: daily cap reached")
        return 0

    max_per_loop = int(cfg.get("research_worker.max_tasks_per_loop", 2))
    max_age = int(cfg.get("research_worker.task_age_max_days", 14))
    tasks = _pending_tasks(max_per_loop, max_age)
    if not tasks:
        return 0

    processed = 0
    for task in tasks:
        result = await _research_one(task)
        attempted_at = datetime.now(UTC).isoformat()
        if result:
            summary, sources = result
            summary = _sanitize_summary(summary)
            with db._conn() as c:
                c.execute(
                    "UPDATE tasks SET research_summary = ?, research_sources_json = ?, "
                    "research_attempted_at = ? WHERE id = ?",
                    (summary, json.dumps(sources), attempted_at, task["id"]),
                )
            processed += 1
        else:
            with db._conn() as c:
                c.execute(
                    "UPDATE tasks SET research_summary = ?, research_attempted_at = ? WHERE id = ?",
                    ("(no useful sources)", attempted_at, task["id"]),
                )

    _bump_loops_today()
    return processed
