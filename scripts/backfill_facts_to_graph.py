"""One-shot backfill: replay historic SQLite facts as Graphiti episodes.

Tables are NOT archived in Phase D; that's a separate post-observation step
once Graphiti reads have been confirmed sound in production for 2+ weeks.

Run once post-deploy:
    uv run python -m scripts.backfill_facts_to_graph
"""
from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime

from graphiti_core.nodes import EpisodeType

from agents import config as cfg
from storage import db
from storage.graph import add_episode_safe


async def main() -> int:
    if db.runtime_get("graph_backfill_done") == "1":
        print("backfill: already done; skip.")
        return 0

    # Cost safety: throttle + hard cap. Each episode triggers ~2-4 DeepSeek
    # entity-extraction calls via OpenRouter. Default cap covers small/medium
    # DBs; large DBs must override via config or run in chunks.
    min_interval_s = float(cfg.get("graph.backfill_min_interval_s", 0.5))
    max_episodes = int(cfg.get("graph.backfill_max_episodes", 500))

    with db._conn() as c:
        rows = c.execute(
            "SELECT id, subject, predicate, object, source, attribution, created_at "
            "FROM facts WHERE valid_to IS NULL ORDER BY id"
        ).fetchall()

    if len(rows) > max_episodes:
        print(
            f"backfill: REFUSED — {len(rows)} live facts exceeds cap "
            f"({max_episodes}). Raise graph.backfill_max_episodes in "
            f"engagement.yaml or run with explicit override."
        )
        return 2

    print(
        f"backfill: {len(rows)} live facts to replay "
        f"(throttle={min_interval_s}s, cap={max_episodes})"
    )
    ok = 0
    fail = 0
    for r in rows:
        body = f"{r['subject']} {r['predicate']} {r['object']}"
        ref_time = _parse_iso(r["created_at"])
        attribution = r["attribution"] or "unknown"
        succ = await add_episode_safe(
            name=f"backfill_fact_{r['id']}",
            episode_body=body,
            source=EpisodeType.text,
            source_description=f"backfill attribution={attribution}",
            reference_time=ref_time,
        )
        if succ:
            ok += 1
        else:
            fail += 1
        if ok % 50 == 0 and ok > 0:
            print(f"  ... {ok} ok, {fail} fail")
        if min_interval_s > 0:
            await asyncio.sleep(min_interval_s)

    print(f"backfill: done. ok={ok} fail={fail}")
    if fail == 0:
        db.runtime_set("graph_backfill_done", "1")
        print("backfill: marked complete in runtime_state.")
    return 0 if fail == 0 else 1


def _parse_iso(s: str | None) -> datetime:
    if not s:
        return datetime.now(UTC)
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return datetime.now(UTC)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
