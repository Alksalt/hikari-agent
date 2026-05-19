"""One-shot backfill: embed any facts/episodes that don't yet have a vec0 entry.

Usage:
    uv run python scripts/backfill_embeddings.py
"""

from __future__ import annotations

import asyncio
import logging
import sys

from storage import db
from tools import embeddings

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill")

BATCH = 64


async def _backfill_facts() -> int:
    ids = db.ids_without_embedding("facts")
    if not ids:
        logger.info("facts: nothing to backfill")
        return 0
    logger.info("facts: %d rows need embedding", len(ids))
    n = 0
    for i in range(0, len(ids), BATCH):
        chunk_ids = ids[i:i + BATCH]
        rows = [db.get_fact(fid) for fid in chunk_ids]
        rows = [r for r in rows if r]
        texts = [f"{r['subject']} {r['predicate']} {r['object']}" for r in rows]
        embs = await embeddings.aembed_batch(texts)
        for r, e in zip(rows, embs, strict=True):
            db.set_vec_fact(r["id"], e)
            n += 1
        logger.info("facts: embedded %d/%d", n, len(ids))
    return n


async def _backfill_episodes() -> int:
    ids = db.ids_without_embedding("episodes")
    if not ids:
        logger.info("episodes: nothing to backfill")
        return 0
    logger.info("episodes: %d rows need embedding", len(ids))
    n = 0
    for i in range(0, len(ids), BATCH):
        chunk_ids = ids[i:i + BATCH]
        rows = [db.get_episode(eid) for eid in chunk_ids]
        rows = [r for r in rows if r]
        texts = [r["summary"] for r in rows]
        embs = await embeddings.aembed_batch(texts)
        for r, e in zip(rows, embs, strict=True):
            db.set_vec_episode(r["id"], e)
            n += 1
        logger.info("episodes: embedded %d/%d", n, len(ids))
    return n


async def main() -> int:
    facts_n = await _backfill_facts()
    episodes_n = await _backfill_episodes()
    logger.info("done: %d facts, %d episodes", facts_n, episodes_n)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
