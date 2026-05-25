"""compound_turn — run multiple tasks extracted from one message.

Builds dependency waves via topological sort: tasks with no unresolved
depends_on run in the first wave (parallel via asyncio.gather); dependent
tasks run in subsequent waves. Each task is dispatched via run_internal_control
(stateless, cheap model, no session resume). The combined result is returned
to the caller which sends it as the reply.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


def _build_waves(tasks: list[dict]) -> list[list[int]]:
    """Topological sort into execution waves (each wave runs in parallel).

    Raises ValueError on dependency cycles.
    """
    n = len(tasks)
    remaining = set(range(n))
    completed: set[int] = set()
    waves: list[list[int]] = []
    while remaining:
        wave = [
            i for i in remaining
            if all(d in completed for d in tasks[i].get("depends_on", []))
        ]
        if not wave:
            raise ValueError(f"dependency cycle in tasks {remaining}")
        waves.append(wave)
        for i in wave:
            remaining.discard(i)
            completed.add(i)
    return waves


async def run_compound_turn(tasks: list[dict]) -> str:
    """Execute tasks in dependency order and return combined results.

    Single-task list: direct run_internal_control, no overhead.
    Multi-task: topological waves, parallel within each wave.
    """
    from agents.runtime import run_internal_control

    if len(tasks) == 1:
        return await run_internal_control(tasks[0]["task"])

    try:
        waves = _build_waves(tasks)
    except ValueError:
        logger.warning("compound_turn: cycle detected — falling back to sequential")
        waves = [[i] for i in range(len(tasks))]

    results: dict[int, str] = {}
    for wave in waves:
        if len(wave) == 1:
            idx = wave[0]
            results[idx] = await run_internal_control(tasks[idx]["task"])
        else:
            wave_results = await asyncio.gather(
                *[run_internal_control(tasks[idx]["task"]) for idx in wave],
                return_exceptions=True,
            )
            for idx, res in zip(wave, wave_results):
                if isinstance(res, Exception):
                    logger.warning("compound_turn: task %d raised: %s", idx, res)
                    results[idx] = f"(task {idx + 1} failed: {res})"
                else:
                    results[idx] = str(res)

    parts = [results[i].strip() for i in range(len(tasks)) if results.get(i, "").strip()]
    return "\n\n".join(parts)
